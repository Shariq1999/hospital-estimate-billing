# Automated Hospital Estimate Billing Generator

A robust, production-grade automated Hospital Cost Estimate and Billing Generator built with **Python**, **Streamlit**, and **SQLite (WAL Mode)**.

---

## 🏥 Features

### 1. Reactive Estimation Generator UI (`app.py`)
- **Cascading Dropdowns (outside `st.form`):** Department selection dynamically filters attending specialists/surgeons and relevant procedures.
- **Auto-Default Length of Stay (LOS):** Automatically sets default stay duration from the procedure tariff, allowing custom doctor/patient overrides (including Day Care with LOS 0).
- **Session State Preservation:** Generated estimates persist across reruns. Modifying input fields flags the estimate as dirty and safely guards against premature saving.
- **Indian Rupee (INR) Formatting:** All rates and financial calculations render using standard Indian numbering system (`₹1,50,000.00`) with whole rupee options (`₹1,50,000`) for clean labels.

### 2. High-Precision Financial Calculation Engine (`billing.py`)
- **Exact Decimal Arithmetic:** Implements `Decimal` with `ROUND_HALF_UP` to prevent floating-point inaccuracies.
- **Surgeon Professional Fees:** Dynamically computed from the procedure base tariff and the attending doctor's fee percentage ($\text{base\_cost} \times \frac{\text{surgeon\_fee\_pct}}{100}$).
- **Proportional Discount Allocation:** Automatically divides discounts proportionally across all line items, allocating leftover paise to the largest line item so itemized net values sum exactly to net base.
- **Taxable Item Segregation & Contingency:**
  - Non-taxed contingency buffer applied strictly to post-discount net.
  - Tax (GST) applied strictly to post-discount taxable items (`consumables_cost`, `investigations_charge`).
- **Range Band:** Configurable $\pm 10\%$ range estimation accounting for clinical variations.
- **Input Validation:** Unicode-compliant patient names (supporting Indian and international scripts), boundary checks, and money/percentage validators.

### 3. Professional PDF Letterhead Generator (`pdf_generator.py`)
- **Bundled Unicode Font:** Ships with `assets/fonts/NotoSans-Regular.ttf` and `NotoSans-Bold.ttf` for reliable rendering of Indian Rupee (`₹`) glyphs and international characters without OS font dependencies.
- **Draft vs. Permanent Watermarking:** Unsaved estimates are stamped with `*** DRAFT - NOT SAVED ***`; saved estimates carry their unique transaction number (`EST-YYYYMMDD-XXXX`).
- **Print Formatting:** 4-column itemized breakdown, spanned totals table, hospital contact header placeholder, and legal clinical disclaimers.

### 4. Immutable Historical Records & Reprint
- **JSON Snapshots:** Historical estimates are saved alongside an immutable snapshot (`line_items_json`) capturing hospital details, clinic metadata, and frozen tariffs.
- **Price Hike Immunity:** Future tariff updates or doctor fee adjustments never mutate past estimates.
- **Historical Validity:** Validity expiration dates are computed strictly from the frozen creation date (`created_at`), never from today's date.
- **Formatted Timestamps:** Displayed as `DD-MMM-YYYY HH:MM`.

### 5. Administrator Portal & Security (`admin_auth`)
- **PBKDF2 Password Hashing:** 600,000 iterations using PBKDF2-HMAC-SHA256 with 16-byte random salts and constant-time `hmac.compare_digest` verification.
- **First-Run Setup & Protection:** First-time onboarding requires creating an admin account (min 8 characters). Login failures include intentional rate-limiting delays against brute-force attempts.
- **Soft Deletes Only:** Zero hard deletes across master tables (`room_types`, `departments`, `doctors`, `procedures`). Records can be deactivated and restored safely (`is_active = 0 / 1`).
- **Duplicate Prevention:** Case-insensitive duplicate checks and `sqlite3.IntegrityError` guards.
- **CSV Import & Export:**
  - UTF-8 with BOM (`utf-8-sig`) encoding for seamless Microsoft Excel compatibility.
  - Formula injection defense: Automatically neutralizes cells beginning with `=`, `+`, `-`, or `@`.
  - Departments referenced by human-readable names.
  - Interactive preview before import with atomic all-or-nothing transactions (add/update only; never wipes existing tables).
- **Hot Database Backup:** Live database snapshots created via SQLite's native backup API (`hospital_backup.db`) behind admin authentication.

---

## 📂 Project Architecture

```
Estimation/
├── assets/
│   └── fonts/
│       ├── NotoSans-Regular.ttf     # Primary Unicode font (includes ₹ glyph)
│       └── NotoSans-Bold.ttf        # Bold font variant
├── tests/
│   ├── __init__.py
│   ├── test_admin.py                # Admin auth, soft-deletes, CSV import/export
│   ├── test_billing.py              # Decimal calculations, discount allocation, INR
│   ├── test_database.py             # Schema, WAL mode, transactional counter
│   ├── test_generator_flow.py       # End-to-end estimate generation flow
│   └── test_immutability.py         # Snapshot immutability & PDF reprint verification
├── .gitignore                       # Ignores database files, WAL logs, cache
├── app.py                           # Main Streamlit web application
├── billing.py                       # Pure Decimal financial calculation engine
├── database.py                      # SQLite database layer, WAL mode, CSV routines
├── pdf_generator.py                 # ReportLab PDF generator
├── pytest.ini                       # Pytest configuration (pythonpath = .)
├── README.md                        # Documentation
├── requirements.txt                 # Pinned dependencies
└── seed_data.py                     # Realistic metro hospital sample dataset
```

---

## 🚀 Getting Started

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.14)
- Git

### 2. Installation
Clone the repository and install dependencies:
```bash
git clone <repository_url>
cd Estimation
pip install -r requirements.txt
```

### 3. Run the Streamlit Application
Launch the web interface:
```bash
streamlit run app.py
```
Open your browser at `http://localhost:8501`.

### 4. First-Time Setup
1. On initial startup, the database schema is automatically created in `hospital.db` in **WAL mode** and seeded with realistic sample data.
2. Navigate to **⚙️ Admin Portal** in the sidebar.
3. Complete the **First-Run Setup** form to set up your primary administrator account.

---

## 🧪 Running Automated Tests

Run the complete test suite with `pytest`:
```bash
pytest
```

To run with verbose output:
```bash
pytest -v
```

All 31 unit and integration tests verify:
- Exact Decimal rounding and leftover paise discount distribution
- Day Care (LOS 0) zero room charges
- Unicode patient name validation
- Concurrency-safe atomic estimate counter (`EST-YYYYMMDD-XXXX`)
- Historical estimate snapshot immutability
- PBKDF2 authentication and password update
- Soft-delete and restoration across all master tables
- Formula injection protection and CSV round-tripping
- Atomic rollback on invalid CSV import rows
