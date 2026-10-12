# Automated Hospital Estimate Billing Generator

A prototype suitable for internal use on a hospital network for generating patient treatment cost estimates, built with **Python**, **Streamlit**, and **SQLite (WAL Mode)**.

> [!NOTE]
> This application is intended as an internal tariff estimation tool. It operates with a **single admin account**, **no audit log**, and **no role-based access control (RBAC)**.

---

## 🏥 Features

### 1. Reactive Estimation Generator UI (`app.py`)
- **Cascading Dropdowns (outside `st.form`):** Selecting a department dynamically filters the attending specialists/surgeons and relevant procedures.
- **Auto-Default Length of Stay (LOS):** Automatically loads the default stay duration from the procedure tariff while allowing custom doctor/patient overrides (including Day Care with LOS 0).
- **Session State Preservation:** Generated estimates persist across reruns in `st.session_state`. Modifying any input field marks the estimate dirty to prevent saving stale data.
- **Indian Rupee (INR) Formatting:** All figures render using Indian digit grouping (`₹1,50,000.00`), with whole rupee labels (`₹1,50,000`) in dropdowns.

### 2. Financial Calculation Engine (`billing.py`)
- **Exact Decimal Arithmetic:** Implements Python's `Decimal` module with `ROUND_HALF_UP` to prevent floating-point rounding errors.
- **Surgeon Professional Fees:** Dynamically computed from the procedure base tariff and the attending doctor's fee percentage ($\text{base\_cost} \times \frac{\text{surgeon\_fee\_pct}}{100}$).
- **Proportional Discount Allocation:** Automatically divides discounts proportionally across all line items, allocating leftover paise to the largest line item so itemized net values sum exactly to net base.
- **Taxable Item Segregation & Contingency:**
  - Non-taxed contingency buffer applied strictly to post-discount net.
  - Tax (GST) applied strictly to post-discount taxable items (`consumables_cost`, `investigations_charge`).
- **Range Band:** Configurable $\pm 10\%$ range estimation accounting for clinical variations.
- **Input Validation:** Unicode-compliant patient names (supporting Indian and international scripts), boundary checks, and money/percentage validators.

### 3. Professional PDF Letterhead Generator (`pdf_generator.py`)
- **Bundled Unicode Font:** Ships with `assets/fonts/NotoSans-Regular.ttf` and `NotoSans-Bold.ttf` for native rendering of Indian Rupee (`₹`) glyphs and Unicode characters without OS font dependencies. The fonts are released under the **SIL Open Font License** (see `assets/fonts/OFL.txt`).
- **Draft vs. Permanent Watermarking:** Unsaved estimates are stamped with `*** DRAFT - NOT SAVED ***`; saved estimates carry their unique transaction number (`EST-YYYYMMDD-XXXX`).
- **Print Formatting:** 4-column itemized breakdown, spanned totals table to prevent label wrapping, hospital contact header placeholder, and legal clinical disclaimers.

### 4. Immutable Historical Records & Reprint
- **JSON Snapshots:** Historical estimates are saved alongside an immutable snapshot (`line_items_json`) capturing hospital details, clinical metadata, and frozen tariffs.
- **Price Hike Immunity:** Future tariff updates or doctor fee adjustments never mutate past estimates.
- **Historical Validity:** Validity expiration dates are computed strictly from the frozen creation date (`created_at`), never from today's date.
- **Formatted Timestamps:** Displayed as `DD-MMM-YYYY HH:MM`.

### 5. Administrator Portal & Security (`admin_auth`)
- **PBKDF2 Password Hashing:** 600,000 iterations using PBKDF2-HMAC-SHA256 with a 16-byte random salt and constant-time `hmac.compare_digest` verification.
- **First-Run Setup & Protection:** First-time onboarding requires creating an administrator account (minimum 8 characters).
- **Basic Login Delay:** On failed login attempts, an intentional 1-second pause (`time.sleep(1)`) acts as a basic deterrent to slow down automated password guessing (note: this is a deterrence delay, not a permanent account lockout).
- **Soft Deletes Only:** Zero hard deletes across master tables (`room_types`, `departments`, `doctors`, `procedures`). Records can be deactivated and restored safely (`is_active = 0 / 1`).
- **Duplicate Prevention:** Case-insensitive duplicate checks and `sqlite3.IntegrityError` guards.
- **CSV Import & Export (behind login):**
  - UTF-8 with BOM (`utf-8-sig`) encoding for seamless Microsoft Excel compatibility.
  - Formula injection defense: Automatically neutralizes cells beginning with `=`, `+`, `-`, or `@`.
  - Departments referenced by human-readable names.
  - Interactive preview before import with atomic all-or-nothing transactions (add/update only; never wipes existing tables).
- **Hot Database Backup (behind login):** Live database snapshots created via SQLite's native backup API (`hospital_backup.db`).

---

## ⚠️ Before Real Use

1. **Replace Sample Tariffs:** All seeded names, tariffs, doctor fees, and room rates are illustrative placeholders for a metro private hospital. Replace them with your hospital's board-approved tariff schedule before generating real estimates.
2. **Confirm GST / Tax Treatment:** Consult your hospital's finance and tax department before adjusting tax rates or enabling category tax flags. The default tax rate is set to **0%** and all tariff categories default to non-taxable (exempt). Under Indian GST regulations, healthcare services provided by clinical establishments are generally exempt from GST; however, **room rent exceeding ₹5,000 per day outside an ICU may attract 5% GST without Input Tax Credit (ITC)**. Finance must confirm this treatment before enabling the taxable flag on Room Accommodation or any other category.
3. **Protect Personal Healthcare Data:** Patient names and clinical procedures are sensitive personal data. Run this application exclusively on a private, secured workstation or intranet within the hospital network. **Never host this on public or unauthenticated cloud environments.**
4. **Secure Database Backups:** Database backups (`hospital_backup.db`) contain patient estimate records and the administrator's PBKDF2 password hash. Store downloaded backups in an encrypted, access-controlled directory.

---

## ⚠️ Known Limitations

- **Consultation Fee Not Billed:** The doctor consultation fee (`consultation_fee`) is stored in the master list for reference but is not added to the surgical estimate total; only the surgeon's professional fee percentage on procedure base cost is billed.
- **Flat Range Band:** The estimate range band is a flat $\pm\%$ percentage applied to the total, rather than item-by-item clinical variance modeling.
- **Single Admin Account:** Supports only a single administrator account. There is no multi-user role-based access control (RBAC), permission tiers, or action audit logging.

---

## 🔑 Forgotten Admin Password

If the administrator password is lost or forgotten, reset it using the bundled CLI script while the Streamlit application is stopped:

```bash
python reset_admin.py
```

1. The script displays the target database and prompts for typed confirmation (`RESET`).
2. It prompts securely for the new password twice using Python's `getpass` module (minimum 8 characters).
3. The password and its hash are never printed or written to logs.

---

## 📂 Project Architecture

```
Estimation/
├── assets/
│   └── fonts/
│       ├── NotoSans-Regular.ttf     # Primary Unicode font (includes ₹ glyph)
│       ├── NotoSans-Bold.ttf        # Bold font variant
│       └── OFL.txt                  # SIL Open Font License 1.1
├── tests/
│   ├── __init__.py
│   ├── test_admin.py                # Admin auth, reset script, soft-deletes, CSV
│   ├── test_app_test.py             # UI automation tests with streamlit.testing.v1.AppTest
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
├── reset_admin.py                   # Emergency CLI admin password reset tool
└── seed_data.py                     # Metro hospital tariff dataset & idempotent seed
```

---

## 🚀 Setup & Execution

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.14.7 on Windows)
- Git

### 2. Environment Setup

It is strongly recommended to install and run inside a Python virtual environment.

#### Windows (PowerShell)
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

#### macOS / Linux
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Running the Web Application
Launch Streamlit from the activated environment:
```bash
streamlit run app.py
```
Open your web browser at `http://localhost:8501`.

### 4. First-Time Initialization
1. Upon first startup, the database schema is created in `hospital.db` in **WAL mode** and populated with initial tariffs. An internal `'seeded'` flag prevents re-seeding if tables are later cleared.
2. Navigate to **⚙️ Admin Portal** in the sidebar.
3. Complete the **First-Run Setup** form to create your administrator password.

---

## 🧪 Running Automated Tests

Run the full test suite using `pytest`:

```bash
pytest -v
```

### Test Suite Results
In our latest run on **Python 3.14.7** (`pytest-9.1.1` on `win32`), all **44 tests passed** in **19.02s**:
- **Authentication & Security:** First-run onboarding, login attempt deterrent delay, password change, and CLI `reset_admin.py` emergency reset.
- **Streamlit AppTest UI Suite (`test_app_test.py`):** First-run onboarding flow, duplicate department error handling (friendly error without exceptions), doctor and procedure add/edit, switching selected doctors reloading corresponding form values, saving without changes leaving master records unchanged, prevention of stale form input leakage across record selections, and CSV upload preview & atomic import.
- **Whole Rupee & Percentage Validation:** Strict rejection of non-whole rupee amounts (paise/decimals) and fractional surgeon fee percentages in admin forms and CSV import with clear messages (`columns are whole rupees`).
- **Category-wise Tax Configuration:** Default non-taxable healthcare exemption, per-category tax toggles in Settings, and tax calculations.
- **Idempotent Seeding & Soft Deletes:** Verification that clearing master tables never triggers re-seeding on restart, plus soft-delete/restore across room types, departments, doctors, and procedures.
- **Duplicate Prevention:** IntegrityError raised on duplicate names.
- **CSV Portability & Injection Defense:** Formula neutralization (`'`, `=`, `+`, `-`, `@`), UTF-8 BOM encoding, department resolution by name, and all-or-nothing rollback on invalid rows.
- **Financial Precision:** Exact Decimal `ROUND_HALF_UP` rounding, leftover paise distribution to the largest line item, Day Care (LOS 0) zero room charges, and Indian digit grouping (`format_inr`).
- **Audit Immutability & Safe PDF:** Historical snapshots and reprints maintain frozen dates and prices against future tariff updates; XML characters escaped (`xml.sax.saxutils.escape`) to prevent ReportLab parsing crashes; embedded Noto Sans font stream decompressing and verifying U+20B9 (`₹`) glyph mapping.
