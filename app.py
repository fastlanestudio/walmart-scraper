"""app.py — Localhost Web Application for Walmart Product Scraper

Runs on Windows at http://localhost:8000.
Provides real-time collection controls, records viewer, live/test provenance indicators,
log inspector, and one-click semicolon-delimited CSV export.

Usage:
    python app.py [--port 8000] [--db data/walmart.db]
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import datetime
from pathlib import Path
from typing import Optional

from aiohttp import web

from core.store import StoreDB, FIELD_NAMES
from core.source_adapter import (
    TestFixtureAdapter,
    LivePlaywrightAdapter,
    WalmartApiAdapter,
    AdapterStatus,
)
from core.walmart_api import walmart_api_manager, WalmartApiStatus
from core.logging_setup import setup_logging
from walmart.config import load_category_urls

logger = logging.getLogger(__name__)

# Global runtime state
CURRENT_STATE = {
    "is_running": False,
    "active_run_id": None,
    "mode": "live_playwright",
    "status_message": "Idle — ready to start live collection",
    "progress_current": 0,
    "progress_total": 0,
    "current_category": "",
    "error": None,
    "cancel_token": {"cancelled": False},
    "current_task": None,
}


DB_KEY = web.AppKey("db", StoreDB)


def get_db(app: web.Application) -> StoreDB:
    return app[DB_KEY]


# ─── API Endpoints ─────────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    """Readiness and health check."""
    return web.json_response({
        "status": "ok",
        "service": "walmart-scraper-localhost",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


async def handle_readiness(request: web.Request) -> web.Response:
    """Inspect readiness of all supported collection adapters."""
    config = dict(request.query)
    test_adapter = TestFixtureAdapter()
    live_adapter = LivePlaywrightAdapter()
    api_adapter = WalmartApiAdapter()

    t_res = await test_adapter.check_readiness(config)
    l_res = await live_adapter.check_readiness(config)
    a_res = await api_adapter.check_readiness(config)

    return web.json_response({
        "adapters": [
            {
                "id": t_res.name,
                "label": "Test Fixture Adapter (~113 Validated Records)",
                "status": t_res.status,
                "message": t_res.message,
                "supports_live": t_res.supports_live,
            },
            {
                "id": l_res.name,
                "label": "Live Playwright Scraper",
                "status": l_res.status,
                "message": l_res.message,
                "supports_live": l_res.supports_live,
            },
            {
                "id": a_res.name,
                "label": "Walmart Developer / Partner API",
                "status": a_res.status,
                "message": a_res.message,
                "supports_live": a_res.supports_live,
            },
        ]
    })


async def handle_get_api_config(request: web.Request) -> web.Response:
    """Return masked Walmart API configuration and verification status."""
    return web.json_response(walmart_api_manager.config.to_masked_dict())


async def handle_save_api_config(request: web.Request) -> web.Response:
    """Save Walmart API settings securely."""
    try:
        data = await request.json()
        cfg = walmart_api_manager.save_config(data)
        return web.json_response({"status": "saved", "config": cfg.to_masked_dict()})
    except ValueError as ve:
        return web.json_response({"error": str(ve)}, status=400)
    except Exception as e:
        logger.exception("Error saving API config")
        return web.json_response({"error": f"Failed to save configuration: {e}"}, status=500)


async def handle_test_api_config(request: web.Request) -> web.Response:
    """Execute authenticated connection test against Walmart API."""
    try:
        zipcode = "10001"
        if request.can_read_body:
            try:
                body = await request.json()
                zipcode = str(body.get("zipcode", "10001")).strip() or "10001"
            except Exception:
                pass
        res = await walmart_api_manager.test_connection(zipcode=zipcode)
        return web.json_response(res)
    except Exception as e:
        logger.exception("Error testing API connection")
        return web.json_response({"error": f"Test connection error: {e}"}, status=500)


async def handle_clear_api_config(request: web.Request) -> web.Response:
    """Clear stored Walmart API credentials."""
    walmart_api_manager.clear_config()
    return web.json_response({"status": "cleared", "config": walmart_api_manager.config.to_masked_dict()})


async def handle_status(request: web.Request) -> web.Response:
    """Return live collection state, statistics, and run metrics."""
    db = get_db(request.app)
    stats = db.get_stats()
    return web.json_response({
        "is_running": CURRENT_STATE["is_running"],
        "active_run_id": CURRENT_STATE["active_run_id"],
        "mode": CURRENT_STATE["mode"],
        "status_message": CURRENT_STATE["status_message"],
        "progress_current": CURRENT_STATE["progress_current"],
        "progress_total": CURRENT_STATE["progress_total"],
        "current_category": CURRENT_STATE["current_category"],
        "error": CURRENT_STATE["error"],
        "stats": stats,
    })


async def handle_records(request: web.Request) -> web.Response:
    """Return paginated, searchable records from SQLite storage."""
    db = get_db(request.app)
    search = request.query.get("search", "")
    is_test_raw = request.query.get("is_test")
    is_test = int(is_test_raw) if is_test_raw in ("0", "1") else None
    limit = min(int(request.query.get("limit", 50)), 200)
    offset = int(request.query.get("offset", 0))

    data = db.get_records(search=search, is_test=is_test, limit=limit, offset=offset)
    return web.json_response(data)


async def handle_export_csv(request: web.Request) -> web.Response:
    """Generate and stream the exact 22-column semicolon-delimited CSV."""
    db = get_db(request.app)
    delimiter = request.query.get("delimiter", ";")
    if delimiter not in (";", ","):
        delimiter = ";"
    run_id = request.query.get("run_id") or None
    is_test_raw = request.query.get("is_test")
    is_test = int(is_test_raw) if is_test_raw in ("0", "1") else None

    csv_data = db.export_csv(run_id=run_id, delimiter=delimiter, is_test=is_test)
    filename = f"walmart_products_{datetime.date.today().isoformat()}.csv"

    return web.Response(
        body=csv_data.encode("utf-8"),
        content_type="text/csv",
        charset="utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


async def handle_start_collection(request: web.Request) -> web.Response:
    """Start an asynchronous collection job."""
    if CURRENT_STATE["is_running"]:
        return web.json_response({"error": "A collection job is already running."}, status=409)

    payload = await request.json()
    mode = payload.get("mode", "live_playwright")
    zipcode = str(payload.get("zipcode", "10001")).strip()
    target_count = int(payload.get("target_count", 50))

    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + zipcode
    db = get_db(request.app)
    db.begin_run(run_id, mode, zipcode)

    CURRENT_STATE["is_running"] = True
    CURRENT_STATE["active_run_id"] = run_id
    CURRENT_STATE["mode"] = mode
    CURRENT_STATE["status_message"] = f"Initializing {mode} collection for ZIP {zipcode}..."
    CURRENT_STATE["progress_current"] = 0
    CURRENT_STATE["progress_total"] = target_count
    CURRENT_STATE["error"] = None
    CURRENT_STATE["cancel_token"] = {"cancelled": False}

    # Launch background task
    CURRENT_STATE["current_task"] = asyncio.create_task(
        _run_collection_task(request.app, payload, run_id)
    )

    return web.json_response({
        "status": "started",
        "run_id": run_id,
        "mode": mode,
        "zipcode": zipcode,
    })


async def handle_stop_collection(request: web.Request) -> web.Response:
    """Cancel / stop active collection."""
    if not CURRENT_STATE["is_running"]:
        return web.json_response({"message": "No active job is currently running."})

    CURRENT_STATE["cancel_token"]["cancelled"] = True
    CURRENT_STATE["status_message"] = "Stopping collection job..."
    if CURRENT_STATE["current_task"]:
        CURRENT_STATE["current_task"].cancel()

    return web.json_response({"status": "cancelled", "message": "Collection cancellation requested."})


async def handle_reset(request: web.Request) -> web.Response:
    """Safely reset local database."""
    if CURRENT_STATE["is_running"]:
        return web.json_response({"error": "Cannot reset database while collection is running."}, status=409)

    db = get_db(request.app)
    db.clear_all()
    CURRENT_STATE["status_message"] = "Database cleared. All local records reset."
    return web.json_response({"status": "cleared", "message": "Database cleared successfully."})


async def handle_logs(request: web.Request) -> web.Response:
    """Return recent log lines for debugging and audit."""
    log_dir = Path("logs")
    log_lines = []
    if log_dir.exists():
        files = sorted(log_dir.glob("*.log.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            latest_file = files[0]
            with open(latest_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
                log_lines = lines[-50:]  # last 50 lines

    formatted = []
    for line in log_lines:
        try:
            formatted.append(json.loads(line))
        except Exception:
            formatted.append({"msg": line.strip()})

    return web.json_response({"logs": formatted})


async def _run_collection_task(app: web.Application, payload: dict, run_id: str) -> None:
    """Execute collection and update SQLite incrementally."""
    db = get_db(app)
    mode = payload.get("mode", "test_fixtures")
    is_test = 1 if mode == "test_fixtures" else 0

    try:
        if mode == "test_fixtures":
            adapter = TestFixtureAdapter()
        elif mode == "live_playwright":
            adapter = LivePlaywrightAdapter()
        elif mode == "walmart_api":
            adapter = WalmartApiAdapter()
        else:
            raise ValueError(f"Unknown mode: {mode}")

        def progress_cb(msg: str, current: int, total: int):
            CURRENT_STATE["status_message"] = msg
            CURRENT_STATE["progress_current"] = current
            if total > 0:
                CURRENT_STATE["progress_total"] = total

        records = await adapter.collect(
            payload,
            callback=progress_cb,
            cancel_token=CURRENT_STATE["cancel_token"],
        )

        # Upsert records incrementally
        for rec in records:
            p_dict = rec.to_dict()
            p_key = db._product_key(p_dict)
            db.upsert_product(p_key, run_id, p_dict, is_test=is_test, data_source=mode)

        db.mark_unseen(run_id)
        db.finish_run(run_id, status="done")
        CURRENT_STATE["status_message"] = f"Collection completed successfully. {len(records)} records stored."
        CURRENT_STATE["progress_current"] = len(records)
    except asyncio.CancelledError:
        logger.info(f"Run {run_id} was cancelled")
        db.finish_run(run_id, status="cancelled")
        CURRENT_STATE["status_message"] = "Collection cancelled by user."
    except Exception as e:
        logger.exception(f"Collection error in run {run_id}: {e}")
        db.finish_run(run_id, status="failed")
        CURRENT_STATE["error"] = str(e)
        CURRENT_STATE["status_message"] = f"Collection halted: {e}"
    finally:
        CURRENT_STATE["is_running"] = False


# ─── Frontend Dashboard (Single Page HTML + CSS + JS) ──────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    """Serve the single-page application dashboard."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Walmart Product Scraper — Localhost Dashboard</title>
  <style>
    :root {
      --primary: #0071dc;
      --primary-dark: #005bb5;
      --secondary: #ffc220;
      --bg: #f4f6f8;
      --surface: #ffffff;
      --border: #e2e8f0;
      --text: #1e293b;
      --text-muted: #64748b;
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; padding: 24px; }
    .container { max-width: 1300px; margin: 0 auto; }
    header { display: flex; align-items: center; justify-content: space-between; padding-bottom: 20px; border-bottom: 2px solid var(--border); margin-bottom: 24px; }
    .logo-badge { display: flex; align-items: center; gap: 12px; }
    .logo-badge h1 { font-size: 1.5rem; font-weight: 700; color: var(--primary); }
    .status-pill { padding: 4px 12px; border-radius: 9999px; font-size: 0.85rem; font-weight: 600; text-transform: uppercase; }
    .pill-idle { background: #e2e8f0; color: #475569; }
    .pill-running { background: #dbeafe; color: #1e40af; animation: pulse 2s infinite; }
    .pill-ready { background: #dcfce7; color: #166534; }
    .pill-blocked { background: #fee2e2; color: #991b1b; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }

    /* Grid Layout */
    .grid { display: grid; grid-template-columns: 340px 1fr; gap: 24px; }
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 24px; }
    .card-title { font-size: 1.1rem; font-weight: 600; margin-bottom: 16px; border-bottom: 1px solid var(--border); padding-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }

    /* Forms */
    .form-group { margin-bottom: 14px; }
    label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 4px; color: var(--text-muted); }
    input, select, textarea { width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.9rem; }
    input:focus, select:focus { outline: 2px solid var(--primary); border-color: transparent; }

    /* Buttons */
    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 9px 16px; border-radius: 6px; font-size: 0.9rem; font-weight: 600; cursor: pointer; border: none; transition: 0.15s ease; width: 100%; }
    .btn-primary { background: var(--primary); color: #fff; }
    .btn-primary:hover { background: var(--primary-dark); }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-secondary { background: #e2e8f0; color: #334155; }
    .btn-secondary:hover { background: #cbd5e1; }
    .btn-export { background: var(--success); color: #fff; }
    .btn-export:hover { background: #059669; }

    /* Progress & Metrics */
    .metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 20px; }
    .metric-box { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; text-align: center; }
    .metric-val { font-size: 1.8rem; font-weight: 700; color: var(--text); }
    .metric-lbl { font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600; margin-top: 4px; }

    .progress-bar { width: 100%; height: 10px; background: #e2e8f0; border-radius: 9999px; overflow: hidden; margin: 12px 0; }
    .progress-fill { height: 100%; background: var(--primary); width: 0%; transition: width 0.3s ease; }

    /* Table */
    .table-container { overflow-x: auto; max-height: 480px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; text-align: left; }
    th { position: sticky; top: 0; background: #f8fafc; border-bottom: 2px solid var(--border); padding: 10px; font-weight: 600; color: var(--text-muted); }
    td { padding: 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
    tr:hover { background: #f8fafc; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
    .tag-live { background: #dcfce7; color: #166534; }
    .tag-test { background: #fef3c7; color: #92400e; }
    .tag-deal { background: #fee2e2; color: #991b1b; }

    /* Toast */
    #toast { position: fixed; bottom: 24px; right: 24px; padding: 12px 20px; border-radius: 6px; color: #fff; font-weight: 600; font-size: 0.9rem; z-index: 1000; box-shadow: 0 4px 6px rgba(0,0,0,0.1); opacity: 0; transition: opacity 0.3s; pointer-events: none; }
    #toast.show { opacity: 1; }

    /* Walmart API Configuration Panel & Badges */
    .api-status-badge { display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; }
    .badge-CONNECTED { background: #dcfce7; color: #166534; }
    .badge-CONFIGURATION_SAVED { background: #e0f2fe; color: #0369a1; }
    .badge-TESTING_CONNECTION { background: #fef3c7; color: #b45309; }
    .badge-NOT_CONFIGURED { background: #f1f5f9; color: #475569; }
    .badge-INVALID_CREDENTIALS { background: #fee2e2; color: #991b1b; }
    .badge-ACCESS_DENIED { background: #fee2e2; color: #991b1b; }
    .badge-RATE_LIMITED { background: #fff7ed; color: #c2410c; }
    .badge-NETWORK_ERROR { background: #fee2e2; color: #991b1b; }
    .badge-API_ERROR { background: #fee2e2; color: #991b1b; }

    .secret-input-wrapper { position: relative; }
    .secret-input-wrapper input { padding-right: 40px; }
    .toggle-secret-btn { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); background: none; border: none; cursor: pointer; font-size: 1rem; color: var(--text-muted); padding: 4px; }
    .toggle-secret-btn:hover { color: var(--text); }

    .banner { padding: 8px 12px; border-radius: 6px; font-size: 0.8rem; margin-bottom: 12px; }
    .banner-info { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; }
    .banner-warn { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="logo-badge">
        <svg width="32" height="32" viewBox="0 0 32 32" fill="#ffc220"><path d="M16 2l3.8 8.8L29 12l-7 6.2L24 28l-8-5.3L8 28l2-9.8L3 12l9.2-1.2z"/></svg>
        <h1>Walmart Scraper — Localhost</h1>
      </div>
      <div>
        <span id="global-status-pill" class="status-pill pill-idle">IDLE</span>
      </div>
    </header>

    <!-- Top Metrics -->
    <div class="metric-grid">
      <div class="metric-box">
        <div id="stat-total" class="metric-val">0</div>
        <div class="metric-lbl">Total Records</div>
      </div>
      <div class="metric-box">
        <div id="stat-live" class="metric-val" style="color: var(--success);">0</div>
        <div class="metric-lbl">Live Genuine Records</div>
      </div>
      <div class="metric-box">
        <div id="stat-test" class="metric-val" style="color: var(--warning);">0</div>
        <div class="metric-lbl">Test Fixtures (~113)</div>
      </div>
      <div class="metric-box">
        <div id="stat-carried" class="metric-val">0</div>
        <div class="metric-lbl">Carried Forward (Retained)</div>
      </div>
    </div>

    <div class="grid">
      <!-- Left Column: Configuration & Controls -->
      <aside>
        <div class="card">
          <div class="card-title">Collection Controls</div>
          <div class="form-group">
            <label>Data Source Mode (Default: Live)</label>
            <select id="cfg-mode" onchange="checkReadiness()">
              <option value="live_playwright" selected>🌐 Live Playwright Scraper (Real Walmart.com Extraction)</option>
              <option value="walmart_api">⚡ Walmart Developer API (Official Partner API)</option>
              <option value="test_fixtures">🧪 Test Fixtures (Offline Validation Mode Only)</option>
            </select>
          </div>

          <!-- Dynamic Configuration Status Card for Walmart API -->
          <div id="api-status-card" style="display: none; background: #ffffff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 14px; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
              <strong id="api-card-title" style="font-size: 0.85rem; color: var(--primary);">Walmart API</strong>
              <span id="api-card-badge" class="api-status-badge badge-NOT_CONFIGURED">NOT CONFIGURED</span>
            </div>
            <div style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 8px; display: grid; grid-template-columns: 1fr 1fr; gap: 4px;">
              <div>Credentials: <strong id="api-card-cred-state" style="color: var(--text);">No</strong></div>
              <div>Verified Access: <strong id="api-card-access" style="color: var(--text);">None</strong></div>
              <div style="grid-column: span 2;">Last Tested: <span id="api-card-time">—</span></div>
            </div>
            <div id="api-card-message" style="font-size: 0.78rem; line-height: 1.4; color: var(--text); background: #f8fafc; padding: 8px; border-radius: 4px; border: 1px solid var(--border); margin-bottom: 10px;">
              Checking API configuration...
            </div>
            <button type="button" id="btn-toggle-api-panel" class="btn btn-secondary" onclick="toggleApiPanel()" style="padding: 6px 12px; font-size: 0.8rem; width: 100%;">
              ⚙️ Configure Walmart API
            </button>
          </div>

          <!-- Readiness Note (for Live Playwright / Test Fixtures) -->
          <div id="readiness-box" style="padding: 10px; border-radius: 6px; font-size: 0.8rem; margin-bottom: 14px; background: #f8fafc; border: 1px solid var(--border);">
            Checking source readiness...
          </div>

          <!-- Expandable Walmart API Configuration Panel -->
          <div id="walmart-api-panel" style="display: none; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px; margin-bottom: 16px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; border-bottom: 1px solid var(--border); padding-bottom: 6px;">
              <strong style="font-size: 0.95rem; color: var(--text);">Walmart API Configuration</strong>
              <button type="button" onclick="toggleApiPanel(false)" style="background: none; border: none; font-size: 1.1rem; cursor: pointer; color: var(--text-muted);">&times;</button>
            </div>

            <!-- Integration Type -->
            <div class="form-group">
              <label>API Integration Type</label>
              <select id="api-integration-type" onchange="onIntegrationTypeChange()">
                <option value="opd_retail" selected>Walmart Online Pickup & Delivery / Affiliate Retail API</option>
                <option value="marketplace">Walmart Marketplace API</option>
              </select>
            </div>

            <!-- Guidance Banner -->
            <div id="api-type-banner" class="banner banner-info">
              ✅ Intended for authorized retail product catalog, store lookup, and localized pricing use cases.
            </div>

            <!-- Environment -->
            <div class="form-group">
              <label>API Environment</label>
              <select id="api-environment">
                <option value="production" selected>Production (Official Gateway)</option>
                <option value="sandbox">Sandbox (Testing / Pre-prod)</option>
              </select>
            </div>

            <!-- Consumer ID -->
            <div id="group-consumer-id" class="form-group">
              <label>Walmart Consumer ID (<code>WM_CONSUMER.ID</code>)</label>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-bottom: 3px;">Organization / Partner UUID issued by Walmart Developer Portal.</div>
              <input type="text" id="api-consumer-id" placeholder="e.g. 12345678-abcd-1234-ef01-123456789abc">
            </div>

            <!-- Client ID -->
            <div class="form-group">
              <label>Client ID</label>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-bottom: 3px;">OAuth 2.0 Client ID for token authentication.</div>
              <input type="text" id="api-client-id" placeholder="e.g. 7890abcd-ef12-3456-7890-abcdef123456">
            </div>

            <!-- Client Secret -->
            <div class="form-group">
              <label>Client Secret</label>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-bottom: 3px;">Client secret key. Stored securely on backend (.secrets/); never exposed.</div>
              <div class="secret-input-wrapper">
                <input type="password" id="api-client-secret" placeholder="Enter Client Secret">
                <button type="button" class="toggle-secret-btn" id="btn-toggle-secret" onclick="toggleSecretVisibility()" title="Show/Hide Secret">👁️</button>
              </div>
            </div>

            <!-- Action Buttons -->
            <div style="display: flex; gap: 6px; margin-top: 14px;">
              <button type="button" id="btn-api-save" class="btn btn-primary" onclick="saveApiConfig()" style="font-size: 0.8rem; padding: 7px 10px;">💾 Save</button>
              <button type="button" id="btn-api-test" class="btn btn-secondary" onclick="testApiConfig()" style="font-size: 0.8rem; padding: 7px 10px;">🧪 Test Connection</button>
              <button type="button" id="btn-api-clear" class="btn btn-danger" onclick="clearApiConfig()" style="font-size: 0.8rem; padding: 7px 10px;">🗑️ Clear</button>
            </div>
          </div>

          <!-- Proxy Settings -->
          <div id="proxy-group" class="form-group" style="background: #f1f5f9; padding: 10px; border-radius: 6px; border: 1px solid var(--border);">
            <label style="color: var(--primary); font-weight: 700;">Proxy Configuration (Required for Live Scrape)</label>
            <input type="text" id="cfg-proxy-host" oninput="checkReadiness()" placeholder="Host e.g. residential.proxy.com" style="margin-bottom: 6px;">
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 6px;">
              <input type="number" id="cfg-proxy-port" placeholder="Port (8080)">
              <input type="text" id="cfg-proxy-user" placeholder="Username">
            </div>
            <input type="password" id="cfg-proxy-pass" placeholder="Password">
          </div>

          <div class="form-group">
            <label>Store ZIP Code</label>
            <input type="text" id="cfg-zipcode" value="10001" placeholder="e.g. 10001">
          </div>

          <div class="form-group">
            <label>Category URL / Browse Feed</label>
            <input type="text" id="cfg-url" value="https://www.walmart.com/browse/food/snacks-cookies-chips/976759_976779" placeholder="https://www.walmart.com/browse/...">
          </div>

          <div class="form-group">
            <label>Target Record Count</label>
            <input type="number" id="cfg-count" value="50" min="1" max="1000">
          </div>

          <div class="form-group">
            <label>CSV Export Delimiter</label>
            <select id="cfg-delimiter">
              <option value=";" selected>Semicolon ( ; ) — Client Requirement</option>
              <option value=",">Comma ( , ) — Standard CSV</option>
            </select>
          </div>

          <div style="display: flex; gap: 8px; margin-top: 16px;">
            <button id="btn-start" class="btn btn-primary" onclick="startCollection()">Start Collection</button>
            <button id="btn-stop" class="btn btn-danger" onclick="stopCollection()" style="display: none;">Stop</button>
          </div>
        </div>

        <!-- Export & Reset -->
        <div class="card">
          <div class="card-title">Export & Maintenance</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
            <button class="btn btn-export" onclick="exportCsv()">📥 Download 22-Field CSV</button>
            <button class="btn btn-secondary" onclick="resetDatabase()">🗑️ Clear Local Records</button>
          </div>
        </div>

        <!-- Live Log Snippet -->
        <div class="card">
          <div class="card-title">Audit Trail & Logs</div>
          <div id="log-viewer" style="background: #0f172a; color: #38bdf8; padding: 12px; border-radius: 6px; font-family: monospace; font-size: 0.75rem; height: 160px; overflow-y: auto;">
            Waiting for events...
          </div>
        </div>
      </aside>

      <!-- Right Column: Progress & Records Table -->
      <main>
        <div class="card">
          <div class="card-title">
            <span>Collection Status</span>
            <span id="active-run-label" style="font-size: 0.8rem; font-weight: 400; color: var(--text-muted);">No active run</span>
          </div>
          <div id="status-text" style="font-size: 0.9rem; margin-bottom: 8px; font-weight: 500;">Idle</div>
          <div class="progress-bar">
            <div id="progress-fill" class="progress-fill"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-muted);">
            <span id="progress-count">0 / 0 items</span>
            <span id="progress-pct">0%</span>
          </div>
        </div>

        <div class="card">
          <div class="card-title">
            <span>Collected Products (22-Field Schema)</span>
            <div style="display: flex; gap: 10px; align-items: center;">
              <input type="text" id="table-search" placeholder="Search title, UPC, category..." style="width: 240px; padding: 4px 8px; font-size: 0.8rem;" oninput="fetchRecords()">
              <select id="filter-type" style="width: 140px; padding: 4px 8px; font-size: 0.8rem;" onchange="fetchRecords()">
                <option value="">All Records</option>
                <option value="1">🧪 Test Only</option>
                <option value="0">🟢 Live Only</option>
              </select>
            </div>
          </div>

          <div class="table-container">
            <table>
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Title</th>
                  <th>ZIP</th>
                  <th>Brand</th>
                  <th>Price</th>
                  <th>Sale</th>
                  <th>PPM</th>
                  <th>Deals</th>
                  <th>UPC</th>
                  <th>Date</th>
                </tr>
              </thead>
              <tbody id="records-tbody">
                <tr><td colspan="10" style="text-align: center; color: var(--text-muted); padding: 24px;">No records collected yet. Click 'Start Collection' to begin.</td></tr>
              </tbody>
            </table>
          </div>

          <!-- Pagination -->
          <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 14px; font-size: 0.85rem;">
            <span id="pagination-info">Showing 0 of 0 records</span>
            <div style="display: flex; gap: 6px;">
              <button class="btn btn-secondary" style="width: auto; padding: 4px 10px;" onclick="prevPage()">Prev</button>
              <button class="btn btn-secondary" style="width: auto; padding: 4px 10px;" onclick="nextPage()">Next</button>
            </div>
          </div>
        </div>
      </main>
    </div>
  </div>

  <div id="toast">Notification</div>

  <script>
    let currentOffset = 0;
    const PAGE_SIZE = 50;
    let totalRecords = 0;
    let currentApiConfig = null;

    function showToast(msg, isError = false) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.background = isError ? 'var(--danger)' : 'var(--success)';
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 3500);
    }

    async function fetchApiConfig() {
      try {
        const res = await fetch('/api/config/walmart-api');
        const data = await res.json();
        currentApiConfig = data;
        renderApiStatus(data);
      } catch (e) {
        console.error('Failed to fetch API config', e);
      }
    }

    function renderApiStatus(cfg) {
      if (!cfg) return;
      const card = document.getElementById('api-status-card');
      const badge = document.getElementById('api-card-badge');
      const title = document.getElementById('api-card-title');
      const credState = document.getElementById('api-card-cred-state');
      const access = document.getElementById('api-card-access');
      const timeLbl = document.getElementById('api-card-time');
      const msg = document.getElementById('api-card-message');

      title.innerText = cfg.integration_type === 'marketplace' ? 'Walmart Marketplace API' : 'Walmart OPD / Retail API';
      badge.className = 'api-status-badge badge-' + cfg.status;
      badge.innerText = cfg.status.replace(/_/g, ' ');

      credState.innerText = cfg.is_configured ? 'Configured ✅' : 'Missing ❌';
      credState.style.color = cfg.is_configured ? 'var(--success)' : 'var(--danger)';

      access.innerText = (cfg.verified_endpoints && cfg.verified_endpoints.length > 0)
        ? cfg.verified_endpoints.join(', ')
        : 'None';

      timeLbl.innerText = cfg.last_test_timestamp ? new Date(cfg.last_test_timestamp).toLocaleTimeString() : 'Never tested';
      msg.innerText = cfg.last_test_message || 'No test results yet.';

      // Populate form values if inputs exist
      if (document.getElementById('api-integration-type')) {
        document.getElementById('api-integration-type').value = cfg.integration_type || 'opd_retail';
        document.getElementById('api-environment').value = cfg.environment || 'production';
        if (cfg.consumer_id) {
          document.getElementById('api-consumer-id').placeholder = cfg.consumer_id;
        }
        if (cfg.client_id) {
          document.getElementById('api-client-id').placeholder = cfg.client_id;
        }
        if (cfg.has_client_secret) {
          document.getElementById('api-client-secret').placeholder = '•••••••••••• (Saved)';
        }
        onIntegrationTypeChange();
      }
    }

    function toggleApiPanel(forceState) {
      const panel = document.getElementById('walmart-api-panel');
      if (!panel) return;
      if (typeof forceState === 'boolean') {
        panel.style.display = forceState ? 'block' : 'none';
      } else {
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
      }
      const btn = document.getElementById('btn-toggle-api-panel');
      if (btn) {
        btn.innerText = panel.style.display === 'block' ? '✕ Close API Settings' : '⚙️ Configure Walmart API';
      }
    }

    function onIntegrationTypeChange() {
      const itype = document.getElementById('api-integration-type').value;
      const groupConsumerId = document.getElementById('group-consumer-id');
      const banner = document.getElementById('api-type-banner');

      if (itype === 'marketplace') {
        groupConsumerId.style.display = 'none';
        banner.className = 'banner banner-warn';
        banner.innerHTML = '⚠️ <strong>Notice:</strong> Marketplace API is intended for 3P seller operations (orders & listings). It does not provide consumer in-store grocery catalog or localized aisle pricing.';
      } else {
        groupConsumerId.style.display = 'block';
        banner.className = 'banner banner-info';
        banner.innerHTML = '✅ Intended for retail product catalog, store lookup, and localized pricing use cases.';
      }
    }

    function toggleSecretVisibility() {
      const input = document.getElementById('api-client-secret');
      const btn = document.getElementById('btn-toggle-secret');
      if (input.type === 'password') {
        input.type = 'text';
        btn.innerText = '🔒';
      } else {
        input.type = 'password';
        btn.innerText = '👁️';
      }
    }

    async function saveApiConfig() {
      const itype = document.getElementById('api-integration-type').value;
      const env = document.getElementById('api-environment').value;
      const consumerId = document.getElementById('api-consumer-id').value.trim();
      const clientId = document.getElementById('api-client-id').value.trim();
      const clientSecret = document.getElementById('api-client-secret').value.trim();

      const btn = document.getElementById('btn-api-save');
      btn.disabled = true;
      btn.innerText = 'Saving...';

      try {
        const res = await fetch('/api/config/walmart-api', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            integration_type: itype,
            environment: env,
            consumer_id: consumerId,
            client_id: clientId,
            client_secret: clientSecret,
          }),
        });
        const data = await res.json();
        if (res.ok) {
          showToast('Walmart API configuration saved securely!');
          document.getElementById('api-client-secret').value = '';
          await fetchApiConfig();
          await checkReadiness();
        } else {
          showToast(data.error || 'Failed to save configuration', true);
        }
      } catch (e) {
        showToast('Error saving: ' + e, true);
      } finally {
        btn.disabled = false;
        btn.innerText = '💾 Save';
      }
    }

    async function testApiConfig() {
      const zipcode = document.getElementById('cfg-zipcode').value.trim() || '10001';
      const btn = document.getElementById('btn-api-test');
      btn.disabled = true;
      btn.innerText = 'Testing...';

      try {
        const res = await fetch('/api/config/walmart-api/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ zipcode }),
        });
        const data = await res.json();
        if (data.status === 'CONNECTED') {
          showToast('Connection verified successfully!');
        } else {
          showToast(data.last_test_message || ('Test status: ' + data.status), true);
        }
        await fetchApiConfig();
        await checkReadiness();
      } catch (e) {
        showToast('Test failed: ' + e, true);
      } finally {
        btn.disabled = false;
        btn.innerText = '🧪 Test Connection';
      }
    }

    async function clearApiConfig() {
      if (!confirm('Are you sure you want to clear all stored Walmart API credentials?')) return;
      try {
        const res = await fetch('/api/config/walmart-api/clear', { method: 'POST' });
        if (res.ok) {
          showToast('Walmart API credentials cleared');
          document.getElementById('api-consumer-id').value = '';
          document.getElementById('api-consumer-id').placeholder = 'e.g. 12345678-abcd-...';
          document.getElementById('api-client-id').value = '';
          document.getElementById('api-client-id').placeholder = 'e.g. 7890abcd-...';
          document.getElementById('api-client-secret').value = '';
          document.getElementById('api-client-secret').placeholder = 'Enter Client Secret';
          await fetchApiConfig();
          await checkReadiness();
        }
      } catch (e) {
        showToast('Error clearing credentials: ' + e, true);
      }
    }

    async function checkReadiness() {
      const mode = document.getElementById('cfg-mode').value;
      const box = document.getElementById('readiness-box');
      const apiCard = document.getElementById('api-status-card');
      const proxyGroup = document.getElementById('proxy-group');

      if (proxyGroup) {
        proxyGroup.style.display = mode === 'live_playwright' ? 'block' : 'none';
      }

      if (mode === 'walmart_api') {
        box.style.display = 'none';
        apiCard.style.display = 'block';
        await fetchApiConfig();
      } else {
        box.style.display = 'block';
        apiCard.style.display = 'none';
        toggleApiPanel(false);
      }

      const proxyHost = document.getElementById('cfg-proxy-host') ? document.getElementById('cfg-proxy-host').value : '';
      try {
        const res = await fetch('/api/readiness?proxy_host=' + encodeURIComponent(proxyHost));
        const data = await res.json();
        const cur = data.adapters.find(a => a.id === mode);
        if (cur && mode !== 'walmart_api') {
          box.innerHTML = `<strong>${cur.label}:</strong> <span style="color: ${cur.status === 'ready' ? 'var(--success)' : 'var(--danger)'}">${cur.status.toUpperCase()}</span><br>${cur.message}`;
        }
      } catch (e) {
        if (box) box.innerText = 'Unable to check readiness: ' + e;
      }
    }

    async function pollStatus() {
      try {
        const res = await fetch('/api/status');
        const s = await res.json();

        // Update Pill
        const pill = document.getElementById('global-status-pill');
        const btnStart = document.getElementById('btn-start');
        const btnStop = document.getElementById('btn-stop');

        if (s.is_running) {
          pill.className = 'status-pill pill-running';
          pill.innerText = 'COLLECTING...';
          btnStart.style.display = 'none';
          btnStop.style.display = 'block';
        } else {
          pill.className = 'status-pill pill-idle';
          pill.innerText = 'IDLE';
          btnStart.style.display = 'block';
          btnStop.style.display = 'none';
        }

        document.getElementById('status-text').innerText = s.status_message;
        document.getElementById('active-run-label').innerText = s.active_run_id ? 'Run ID: ' + s.active_run_id : 'No active run';

        // Progress
        const pct = s.progress_total > 0 ? Math.min(100, Math.round((s.progress_current / s.progress_total) * 100)) : 0;
        document.getElementById('progress-fill').style.width = pct + '%';
        document.getElementById('progress-pct').innerText = pct + '%';
        document.getElementById('progress-count').innerText = `${s.progress_current} / ${s.progress_total} items`;

        // Metrics
        if (s.stats) {
          document.getElementById('stat-total').innerText = s.stats.total_records;
          document.getElementById('stat-live').innerText = s.stats.live_records;
          document.getElementById('stat-test').innerText = s.stats.test_records;
          document.getElementById('stat-carried').innerText = s.stats.carried_forward;
        }
      } catch (e) {}
    }

    async function fetchRecords() {
      const search = document.getElementById('table-search').value;
      const isTest = document.getElementById('filter-type').value;
      const url = `/api/records?search=${encodeURIComponent(search)}&is_test=${encodeURIComponent(isTest)}&limit=${PAGE_SIZE}&offset=${currentOffset}`;
      try {
        const res = await fetch(url);
        const data = await res.json();
        totalRecords = data.total;

        const tbody = document.getElementById('records-tbody');
        if (data.records.length === 0) {
          tbody.innerHTML = '<tr><td colspan="10" style="text-align: center; color: var(--text-muted); padding: 24px;">No matching records found.</td></tr>';
        } else {
          tbody.innerHTML = data.records.map(r => `
            <tr>
              <td><span class="tag ${r._is_test ? 'tag-test' : 'tag-live'}">${r._is_test ? 'TEST' : 'LIVE'}</span></td>
              <td title="${r.product_title}"><strong>${(r.product_title || '').substring(0, 36)}...</strong></td>
              <td>${r.zipcode || ''}</td>
              <td>${r.store_brand || '—'}</td>
              <td>${r.regular_price || '—'}</td>
              <td style="color: var(--danger); font-weight: 600;">${r.sale_price || '—'}</td>
              <td><code>${r.ppm || '—'}</code></td>
              <td>${r.volume_deals_description ? '<span class="tag tag-deal">' + r.volume_deals_description + '</span>' : (r.digital_deals_description ? '<span class="tag tag-deal">' + r.digital_deals_description + '</span>' : '—')}</td>
              <td><code>${r.upc || '—'}</code></td>
              <td>${r.crawl_date || ''}</td>
            </tr>
          `).join('');
        }
        document.getElementById('pagination-info').innerText = `Showing ${Math.min(totalRecords, currentOffset + 1)} to ${Math.min(totalRecords, currentOffset + data.records.length)} of ${totalRecords} records`;
      } catch (e) {}
    }

    async function fetchLogs() {
      try {
        const res = await fetch('/api/logs');
        const data = await res.json();
        const viewer = document.getElementById('log-viewer');
        if (data.logs.length > 0) {
          viewer.innerHTML = data.logs.map(l => `[${(l.ts || '').substring(11, 19)}] ${l.level || 'INFO'}: ${l.msg || ''}`).join('<br>');
          viewer.scrollTop = viewer.scrollHeight;
        }
      } catch (e) {}
    }

    async function startCollection() {
      const mode = document.getElementById('cfg-mode').value;
      const zipcode = document.getElementById('cfg-zipcode').value;
      const count = document.getElementById('cfg-count').value;
      const catUrl = document.getElementById('cfg-url').value;
      const proxyHost = document.getElementById('cfg-proxy-host') ? document.getElementById('cfg-proxy-host').value.trim() : '';
      const proxyPort = document.getElementById('cfg-proxy-port') ? document.getElementById('cfg-proxy-port').value.trim() : '';
      const proxyUser = document.getElementById('cfg-proxy-user') ? document.getElementById('cfg-proxy-user').value.trim() : '';
      const proxyPass = document.getElementById('cfg-proxy-pass') ? document.getElementById('cfg-proxy-pass').value.trim() : '';

      try {
        const res = await fetch('/api/collection/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            mode,
            zipcode,
            target_count: count,
            category_url: catUrl,
            proxy_host: proxyHost,
            proxy_port: proxyPort ? parseInt(proxyPort) : undefined,
            proxy_user: proxyUser,
            proxy_pass: proxyPass,
          }),
        });
        const d = await res.json();
        if (res.ok) {
          showToast(`Started ${mode} collection!`);
        } else {
          showToast(d.error || 'Failed to start', true);
        }
      } catch (e) {
        showToast('Error starting: ' + e, true);
      }
    }

    async function stopCollection() {
      await fetch('/api/collection/stop', { method: 'POST' });
      showToast('Cancellation requested');
    }

    async function resetDatabase() {
      if (!confirm('Are you sure you want to clear all stored product records?')) return;
      await fetch('/api/reset', { method: 'POST' });
      showToast('Database cleared');
      currentOffset = 0;
      fetchRecords();
    }

    function exportCsv() {
      const delimiter = document.getElementById('cfg-delimiter').value;
      window.location.href = `/api/export/csv?delimiter=${encodeURIComponent(delimiter)}`;
    }

    function prevPage() {
      if (currentOffset > 0) {
        currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
        fetchRecords();
      }
    }

    function nextPage() {
      if (currentOffset + PAGE_SIZE < totalRecords) {
        currentOffset += PAGE_SIZE;
        fetchRecords();
      }
    }

    // Startup
    fetchApiConfig();
    checkReadiness();
    pollStatus();
    fetchRecords();
    fetchLogs();
    setInterval(pollStatus, 1500);
    setInterval(fetchRecords, 3000);
    setInterval(fetchLogs, 3000);
  </script>
</body>
</html>
"""
    return web.Response(text=html, content_type="text/html", charset="utf-8")


# ─── Application Factory ───────────────────────────────────────────────────────

def create_app(db_path: str = "data/walmart.db") -> web.Application:
    app = web.Application()
    app[DB_KEY] = StoreDB(db_path)

    # Routes
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/readiness", handle_readiness)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/records", handle_records)
    app.router.add_get("/api/export/csv", handle_export_csv)
    app.router.add_get("/api/logs", handle_logs)
    app.router.add_get("/api/config/walmart-api", handle_get_api_config)
    app.router.add_post("/api/config/walmart-api", handle_save_api_config)
    app.router.add_post("/api/config/walmart-api/test", handle_test_api_config)
    app.router.add_post("/api/config/walmart-api/clear", handle_clear_api_config)
    app.router.add_post("/api/collection/start", handle_start_collection)
    app.router.add_post("/api/collection/stop", handle_stop_collection)
    app.router.add_post("/api/reset", handle_reset)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walmart Scraper Localhost Server")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--db", default="data/walmart.db", help="Path to SQLite DB (default: data/walmart.db)")
    args = parser.parse_args()

    app = create_app(args.db)
    print(f"================================================================")
    print(f"  Walmart Scraper Dashboard running at: http://localhost:{args.port}")
    print(f"  Database: {args.db}")
    print(f"================================================================")
    web.run_app(app, host=args.host, port=args.port)
