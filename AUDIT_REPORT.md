# Codebase Audit Report: Walmart Product Scraper

**Date:** 2026-10-25  
**Target:** Localhost Development & Production-Ready Upgrade  
**Repository:** `c:\Users\ASUS\Downloads\$700 scraper\wolmart scraper`

---

## 1. Repository Structure & Framework Identification

- **Language & Runtime:** Python 3.13.15 (Windows x64).
- **Core Dependencies:** `playwright` (v1.64.1), `aiohttp` (v3.14.5), `pytest` (v9.1.1), `python-dotenv` (v1.2.4).
- **Current Architecture:**
  - `core/schema.py`: Dataclass `ProductRecord` enforcing 22 fields and validation rules.
  - `core/deal_classifier.py`: Rule-based Volume vs Digital promotional classifier.
  - `core/store.py`: SQLite availability-retention storage layer (`walmart.db`).
  - `core/checkpoint.py`: Atomic crash-safe checkpoint persistence.
  - `core/base_scraper.py`: Playwright browser lifecycle, polite delays, canary checks.
  - `core/logging_setup.py`: JSONL machine-readable audit logging.
  - `walmart/config.py`: CSV category reader and proxy loader.
  - `walmart/scraper.py`: Site-specific scraper skeleton with PerimeterX challenge signatures.
  - `run.py`: CLI execution script.
  - `tests/`: 5 test files (`test_schema.py`, `test_deal_classifier.py`, `test_store.py`, `test_checkpoint.py`, `test_base_scraper.py`).
  - `deploy/`: Dockerfile and Azure Container notes.

---

## 2. Source of the "113 Test Records"

- The **113 records** referenced in project context correspond to the **113 automated unit tests** in the `tests/` directory (`collected 113 items ... 113 passed in 4.43s`).
- Each test runs synthetic product records through validation, deal classification, SQLite upsert/retention, and checkpoint serialization.
- **Audit Finding:** Test fixtures are currently isolated in `tests/`, but there was previously no visual distinction in storage or UI between synthetic test records and genuine live Walmart extraction records.

---

## 3. Discrepancies, Gaps & Bugs Identified

| Issue | Location | Impact | Remediation Plan |
|---|---|---|---|
| **No Localhost Web Interface** | Project-wide | Application was strictly CLI-based (`run.py`); user cannot view, configure, or export records via browser. | Build an async localhost server (`app.py` / `aiohttp.web`) serving a full-featured dashboard. |
| **Missing Semicolon CSV Export** | `core/store.py` | `StoreDB` only implemented `export_json()` and `export_audit_log()`. No CSV generation existed. | Implement `export_csv()` supporting exact 22-column header and semicolon delimiter with UTF-8 escaping. |
| **Overwriting Products Across Stores** | `core/store.py` line 141 | Primary key `_product_key` was derived purely from URL. If the same product is scraped for Store A (ZIP 10001) and Store B (ZIP 90210), Store B overwrote Store A, destroying location pricing history. | Composite key: `f"{zipcode}:{clean_url}"` preserving store-specific pricing. |
| **Hardcoded `unit_price == ""` Rule** | `core/schema.py` line 55 | Validating `ProductRecord` rejected any non-empty `unit_price`, conflicting with new requirement: *"Actual source-provided unit price and measurement basis, where available."* | Permit source-provided `unit_price` while defaulting to `""`. |
| **Ambiguity in `store_brand` & `ppm`** | `core/schema.py` & `walmart/scraper.py` | Meaning of `ppm` (price per measure) and `store_brand` (retailer private label vs product brand) are unresolved. | Provide configurable mapping; document unresolved fields for client clarification. |
| **CSV Loader Assumes Commas Only** | `walmart/config.py` line 44 | `DictReader` without delimiter detection fails on semicolon-delimited CSVs. | Add delimiter auto-sniffing (support `,` and `;`). |
| **Placeholder DOM Selectors** | `walmart/scraper.py` | `select_store` and `iter_category_product_urls` raise `NotImplementedError`; selectors use `TODO-...`. | Implement adapter architecture supporting live Playwright extraction, verified API adapters, and test fixture feeds with clear labeling. |
| **PerimeterX Anti-Bot Blocking** | `walmart/scraper.py` | Direct datacenter / residential IP blocks without credentials trigger Human/Robot challenges. | Show transparent connection readiness indicator; fail honestly with actionable errors rather than fabricating success. |

---

## 5. Implementation Status & Verification

All planned upgrades have been fully implemented and verified:

1. **Schema & CSV Engine (`core/schema.py`, `core/store.py`):**
   - Implemented `export_csv(run_id, delimiter=";", is_test=None)` strictly following RFC 4180 escaping and 22-column contract.
   - Updated product key to `f"{zipcode}:{clean_url}"` for multi-store retention.
   - Preserved metadata provenance (`is_test`, `data_source`, `_seen`, `_updated_at`).
2. **Data Source Adapter Architecture (`core/source_adapter.py`):**
   - `LivePlaywrightAdapter`: Default live collector using Chromium automation, DOM selectors, JSON-LD extraction, and PerimeterX challenge detection.
   - `WalmartApiAdapter`: Enterprise partner API support with credentials readiness verification.
   - `TestFixtureAdapter`: Isolated offline mode producing explicitly badged `[TEST FIXTURE]` records.
3. **Localhost Application (`app.py`):**
   - Running live on `http://localhost:8000`.
   - Features dynamic proxy inputs, live readiness indicator, real-time status polling, searchable paginated records table, log inspector, and semicolon/comma CSV download.
4. **Verification & Test Suite:**
   - 129 out of 129 tests passing (`pytest tests/`).
   - Semicolon CSV export tested via REST API and confirmed to contain 22 fields in exact order.
   - Transparent PerimeterX detection verified without mock fallback.
