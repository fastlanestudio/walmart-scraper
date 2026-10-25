# Walmart Product Scraper — Localhost Dashboard & Pipeline

Production-ready Walmart product scraper with a local web dashboard, crash-safe resume, SQLite retention store, and exact 22-field semicolon-delimited CSV generation.

---

## 1. Prerequisites

- **Operating System:** Windows 10 / 11 (or Linux/macOS)
- **Python:** Python 3.11+ (tested on Python 3.13.15)
- **PowerShell** or Command Prompt

---

## 2. Installation & Dependency Setup

1. Open PowerShell and navigate to the project directory:
   ```powershell
   cd 'c:\Users\ASUS\Downloads\$700 scraper\wolmart scraper'
   ```

2. Install Python dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

3. Install Playwright Chromium browser binaries:
   ```powershell
   playwright install chromium
   ```

---

## 3. Environment & API Configuration

Configuration can be performed directly through the **Localhost Web Dashboard** without manually modifying files, or via environment variables:

### A. Via Localhost Dashboard (`http://localhost:8000`):
1. Switch **Data Source Mode** to `⚡ Walmart Developer API`.
2. Click **⚙️ Configure Walmart API** to open the configuration panel.
3. Select your integration type:
   - **Walmart OPD / Affiliate Retail API:** Intended for retail catalog, store lookup, and localized pricing. Requires `Walmart Consumer ID (WM_CONSUMER.ID)`, `Client ID`, and `Client Secret`.
   - **Walmart Marketplace API:** Intended for 3P marketplace seller inventory and orders. Requires `Client ID` and `Client Secret`.
4. Click **💾 Save** (credentials are saved securely on backend in `.secrets/walmart_api.json` and masked in the UI).
5. Click **🧪 Test Connection** to verify live OAuth token generation and store locator access.

### B. Via Environment Variables (`.env` or `.env.local`):
```env
# Residential Rotating Proxy (for Live Playwright mode)
PROXY_HOST=residential.proxy-provider.com
PROXY_PORT=8080
PROXY_USER=username
PROXY_PASS=password

# Walmart API Credentials
WALMART_INTEGRATION_TYPE=opd_retail
WALMART_API_ENV=production
WALMART_CONSUMER_ID=your-consumer-id-uuid
WALMART_CLIENT_ID=your-oauth-client-id
WALMART_CLIENT_SECRET=your-oauth-client-secret
```

---

## 4. How to Start the Localhost Application

Start the web application server:
```powershell
python app.py
```
*(Alternatively, you can run: `python run.py --serve`)*

---

## 5. Exact Localhost URL

Open your web browser and navigate to:
```text
http://localhost:8000
```

---

## 6. How to Run Automated Tests

Run the complete 138-test test suite:
```powershell
python -m pytest tests/ -v
```

---

## 7. How to Start a Collection Job

### Via Localhost Web Interface (`http://localhost:8000`):
1. In the **Collection Controls** panel on the left:
   - Select the **Data Source Mode**:
     - `🧪 Test Fixtures (~113 Validated Records)`: Default offline mode for testing, validation, and demo without live scraping restrictions. Clearly labeled with `[TEST]` badges.
     - `🌐 Live Playwright Scraper`: Uses Playwright to crawl live Walmart.com pages (requires residential rotating proxy).
     - `⚡ Walmart Developer API`: Uses official Walmart API credentials if configured.
   - Enter your target **Store ZIP Code** (e.g. `10001`).
   - Specify your **Category URL** or feed.
   - Set the **Target Record Count** (default: `113`).
   - Select your **CSV Delimiter** (default: `;` semicolon).
2. Click **Start Collection**.
3. Watch real-time progress on the progress bar and observe records streaming into the table.

### Via Command Line:
```powershell
python run.py --zipcode 10001 --csv Walmart_Category_URLs.csv --mode simulated
```

---

## 8. How to Export the CSV

1. In the web dashboard under **Export & Maintenance**, click **📥 Download 22-Field CSV**.
2. Or open the direct URL in your browser:
   ```text
   http://localhost:8000/api/export/csv?delimiter=;
   ```
3. The downloaded file matches the client's exact 22-field semicolon-delimited specification:
   ```text
   zipcode;store_name;store_location;store_logo;store_brand;category;sub_category;product_title;weight;regular_price;sale_price;volume_deals_description;volume_deals_price;digital_deals_description;digital_deals_price;image_url;url;upc;gupc;ppm;unit_price;crawl_date
   ```

---

## 9. 22-Field Client Schema & Mapping Reference

| Index | Field Name | Status / Source | Mapping Rule |
|---|---|---|---|
| 1 | `zipcode` | Supported | 5-digit US ZIP with leading zeros preserved (e.g. `"07001"`). |
| 2 | `store_name` | Supported | Verified Walmart store name. |
| 3 | `store_location` | Supported | Verified physical store address. |
| 4 | `store_logo` | Supported | Official Walmart branding URL when present. |
| 5 | `store_brand` | Conditional | Matches private labels (Great Value, Equate, Mainstays) or product brand. *(Ambiguity documented for client)* |
| 6 | `category` | Supported | Primary department / category. |
| 7 | `sub_category` | Supported | Sub-department / aisle classification. |
| 8 | `product_title` | Supported | Full verbatim product title. |
| 9 | `weight` | Supported | Pack size or weight with unit (`oz`, `lb`, `ct`, `fl oz`). Approximate weights (`approx`) are rejected. |
| 10 | `regular_price` | Supported | Verbatim regular unit price (e.g. `"$2.98"`). |
| 11 | `sale_price` | Supported | Verbatim discounted price when on sale. |
| 12 | `volume_deals_description` | Supported | Deal wording (e.g. `"2 for $5"`, `"Rollback"`, `"Buy 1 Get 1 Free"`). |
| 13 | `volume_deals_price` | Supported | Verbatim volume deal price when displayed. |
| 14 | `digital_deals_description`| Supported | Coupon text (e.g. `"Digital coupon save $1.50"`). |
| 15 | `digital_deals_price` | Supported | Verbatim digital coupon discount price. |
| 16 | `image_url` | Supported | Primary high-resolution product image URL. |
| 17 | `url` | Supported | Canonical product page URL. |
| 18 | `upc` | Supported | 12-digit UPC with leading zeros preserved. |
| 19 | `gupc` | Downstream / Unresolved | Blank `""` by default; reserved for client global catalog mapping. |
| 20 | `ppm` | Unresolved / Verbatim | Price per measure verbatim (e.g. `"$0.25/oz"`). Never computed. *(Meaning unresolved by client)* |
| 21 | `unit_price` | Downstream / Supported | Source-provided unit price string or blank `""`. |
| 22 | `crawl_date` | Supported | Collection timestamp in ISO 8601 format (`YYYY-MM-DD`). |

---

## 10. How to Diagnose Common Errors

- **`ERROR: file or directory not found: tests/`:**
  You ran the command from `C:\WINDOWS\system32`. Change directory into the project folder first:
  `cd 'c:\Users\ASUS\Downloads\$700 scraper\wolmart scraper'`
- **`Walmart PerimeterX bot challenge active / Human Verification overlay`:**
  Walmart blocks datacenter and unauthenticated direct connections. To run live crawls, configure residential rotating proxy credentials in `.env` or in the web UI. For local interface and pipeline testing, use `🧪 Test Fixtures` mode.
- **Port 8000 already in use:**
  Run on an alternate port:
  `python app.py --port 8080`

---

## 11. How to Stop the Application

- In PowerShell: Press `Ctrl + C` in the terminal running `python app.py`.
- In the Web UI: Click the red **Stop** button to cancel an active background collection task safely.
