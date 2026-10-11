"""
seed_data.py - Illustrative INR seed data for the Estimate Billing Generator.

IMPORTANT
- All names, rates and prices are PLACEHOLDERS in the range of a private
  multispecialty hospital in a metro city. Replace with your hospital's
  approved tariff before any real use.
- All doctor names are fictional.
- Amounts are whole rupees (int). Convert to Decimal / paise in billing.py.
- base_cost = hospital procedure charge EXCLUDING the surgeon's professional
  fee. The surgeon fee is computed as base_cost * surgeon_fee_pct / 100.
- default_stay_days = 0 means day care (no room charge).
- admin_password is intentionally NOT seeded. Set a hashed password on
  first login.
- Column names assume the revised schema. If your database.py differs,
  adapt the INSERT statements in seed().
"""

import sqlite3
from database import init_db, get_connection

# ---------------------------------------------------------------- settings
SETTINGS = {
    "hospital_name": "Sample Multispeciality Hospital",
    "hospital_address": "Plot 12, Health City Road, Sample Nagar, Mumbai - 400 001",
    "hospital_phone": "+91 22 0000 0000",
    "hospital_email": "billing@samplehospital.example",
    "currency_symbol": "₹",
    "tax_rate": "0",            # confirm GST treatment with finance before changing
    "contingency_rate": "10",   # % applied on post-discount net, not taxed
    "estimate_range_pct": "10", # +/- band shown as min / max estimate
    "validity_days": "15",
    "disclaimer_text": (
        "This is an estimate based on the standard treatment protocol and the "
        "selected room category. Final charges may vary with the patient's "
        "clinical condition, complications, change in length of stay, "
        "additional investigations or procedures, and implants/consumables "
        "actually used. This estimate is not a final bill."
    ),
}

# ------------------------------------------------------------- room types
# (name, daily_rate in INR, description)
ROOM_TYPES = [
    ("General Ward",       2000, "Multi-bed ward, shared facilities"),
    ("Twin Sharing Room",  3500, "Two-bed room with attached bathroom"),
    ("Single Private Room", 5500, "Single room, attendant bed, TV"),
    ("Deluxe Room",        8500, "Large single room with sofa and refrigerator"),
    ("ICU",               12000, "Intensive care bed, 1:1 nursing"),
]

# ------------------------------------------------------------ departments
DEPARTMENTS = [
    "Cardiology",
    "Orthopaedics",
    "General Surgery",
    "Neurosurgery",
    "Obstetrics & Gynaecology",
    "Urology",
]

# ---------------------------------------------------------------- doctors
# (name, department, consultation_fee, surgeon_fee_pct, specialization)
DOCTORS = [
    # Cardiology
    ("Dr. Arvind Menon",    "Cardiology", 1500, 50, "Interventional Cardiologist"),
    ("Dr. Neha Kulkarni",   "Cardiology", 1200, 40, "Interventional Cardiologist"),
    ("Dr. Rajiv Shetty",    "Cardiology", 1000, 40, "Cardiothoracic Surgeon"),
    # Orthopaedics
    ("Dr. Sanjay Deshmukh", "Orthopaedics", 1200, 50, "Joint Replacement Surgeon"),
    ("Dr. Farhan Qureshi",  "Orthopaedics", 1000, 40, "Sports Medicine & Arthroscopy"),
    ("Dr. Meera Iyer",      "Orthopaedics",  800, 30, "Orthopaedic Trauma Surgeon"),
    # General Surgery
    ("Dr. Prakash Nair",    "General Surgery", 1000, 40, "Laparoscopic & GI Surgeon"),
    ("Dr. Anita Joshi",     "General Surgery",  800, 35, "General & Endocrine Surgeon"),
    ("Dr. Vikram Sethi",    "General Surgery",  700, 30, "General Surgeon"),
    # Neurosurgery
    ("Dr. Karthik Rao",     "Neurosurgery", 1500, 50, "Neurosurgeon (Brain & Skull Base)"),
    ("Dr. Shalini Bhat",    "Neurosurgery", 1200, 40, "Spine Surgeon"),
    ("Dr. Imran Sheikh",    "Neurosurgery", 1000, 35, "Neurosurgeon"),
    # Obstetrics & Gynaecology
    ("Dr. Kavita Pillai",   "Obstetrics & Gynaecology", 1000, 40, "Obstetrician & Gynaecologist"),
    ("Dr. Sunita Agarwal",  "Obstetrics & Gynaecology",  900, 35, "Laparoscopic Gynaecologist"),
    ("Dr. Rhea D'Souza",    "Obstetrics & Gynaecology",  700, 30, "Obstetrician"),
    # Urology
    ("Dr. Manoj Verma",     "Urology", 1200, 45, "Urologist & Endourologist"),
    ("Dr. Pooja Bansal",    "Urology", 1000, 40, "Urologist"),
    ("Dr. Alok Thakur",     "Urology",  800, 30, "Urologist"),
]

# ------------------------------------------------------------- procedures
# (name, department, base_cost, ot_charge, anaesthesia_charge,
#  consumables_cost, investigations_charge, default_stay_days)
PROCEDURES = [
    # Cardiology
    ("Coronary Angiography",              "Cardiology",  8000,  6000,     0,   9000,  4000, 1),
    ("PTCA - Single Stent (DES)",         "Cardiology", 60000, 25000,  8000, 120000,  8000, 3),
    ("Pacemaker Implantation (Dual Chamber)", "Cardiology", 50000, 20000, 6000, 250000, 8000, 3),
    ("CABG (2-3 Grafts)",                 "Cardiology", 150000, 60000, 35000, 120000, 20000, 9),
    # Orthopaedics
    ("Total Knee Replacement (Unilateral)", "Orthopaedics", 90000, 35000, 15000, 160000, 10000, 5),
    ("Total Hip Replacement",             "Orthopaedics", 100000, 40000, 15000, 180000, 10000, 5),
    ("ACL Reconstruction (Arthroscopic)", "Orthopaedics", 60000, 30000, 12000,  70000,  8000, 2),
    ("Femur Fracture Fixation (IM Nail)", "Orthopaedics", 50000, 25000, 12000,  55000,  8000, 4),
    # General Surgery
    ("Laparoscopic Cholecystectomy",      "General Surgery", 35000, 18000, 8000, 20000, 5000, 2),
    ("Laparoscopic Hernia Repair (Mesh)", "General Surgery", 30000, 15000, 7000, 25000, 4000, 2),
    ("Laparoscopic Appendectomy",         "General Surgery", 30000, 15000, 7000, 15000, 4000, 2),
    ("Total Thyroidectomy",               "General Surgery", 45000, 20000, 9000, 12000, 6000, 3),
    # Neurosurgery
    ("Craniotomy - Tumour Excision",      "Neurosurgery", 120000, 50000, 30000,  80000, 25000, 8),
    ("Lumbar Microdiscectomy",            "Neurosurgery",  55000, 25000, 12000,  30000, 10000, 3),
    ("Burr Hole Drainage (Chronic SDH)",  "Neurosurgery",  40000, 20000, 10000,  15000,  8000, 5),
    ("Spinal Fusion (Single Level)",      "Neurosurgery",  90000, 35000, 15000, 150000, 12000, 5),
    # Obstetrics & Gynaecology
    ("Normal Vaginal Delivery",           "Obstetrics & Gynaecology", 15000,  8000,     0,  8000, 4000, 2),
    ("Caesarean Section (LSCS)",          "Obstetrics & Gynaecology", 35000, 18000, 10000, 15000, 6000, 4),
    ("Total Laparoscopic Hysterectomy",   "Obstetrics & Gynaecology", 55000, 25000, 10000, 25000, 7000, 3),
    ("Laparoscopic Ovarian Cystectomy",   "Obstetrics & Gynaecology", 40000, 20000,  9000, 15000, 6000, 2),
    # Urology
    ("TURP (Prostate Resection)",         "Urology", 45000, 22000,  9000, 20000, 6000, 3),
    ("PCNL (Kidney Stone Removal)",       "Urology", 55000, 25000, 10000, 35000, 8000, 3),
    ("RIRS with Laser Lithotripsy",       "Urology", 50000, 25000, 10000, 40000, 6000, 2),
    ("Cystoscopy with DJ Stent",          "Urology", 20000, 12000,  6000, 15000, 3000, 1),
]


def seed(conn: sqlite3.Connection) -> None:
    """Insert seed rows. Safe to re-run: skips if seeded flag is set."""
    init_db(conn)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")

    row = cur.execute("SELECT value FROM settings WHERE key = 'seeded'").fetchone()
    if row and row[0] == "1":
        return

    if cur.execute("SELECT COUNT(*) FROM departments").fetchone()[0] > 0:
        cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('seeded', '1')")
        conn.commit()
        return

    for k, v in SETTINGS.items():
        cur.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )

    for name, rate, desc in ROOM_TYPES:
        cur.execute(
            "INSERT INTO room_types (name, daily_rate, description, is_active) "
            "VALUES (?, ?, ?, 1)",
            (name, rate, desc),
        )

    dept_ids = {}
    for name in DEPARTMENTS:
        cur.execute(
            "INSERT INTO departments (name, is_active) VALUES (?, 1)", (name,)
        )
        dept_ids[name] = cur.lastrowid

    for name, dept, cons_fee, pct, spec in DOCTORS:
        cur.execute(
            "INSERT INTO doctors (name, department_id, consultation_fee, "
            "surgeon_fee_pct, specialization, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (name, dept_ids[dept], cons_fee, pct, spec),
        )

    for (name, dept, base, ot, anaes, cons, inv, stay) in PROCEDURES:
        cur.execute(
            "INSERT INTO procedures (name, department_id, base_cost, ot_charge, "
            "anaesthesia_charge, consumables_cost, investigations_charge, "
            "default_stay_days, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (name, dept_ids[dept], base, ot, anaes, cons, inv, stay),
        )

    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('seeded', '1')")
    conn.commit()


if __name__ == "__main__":
    # Sanity check on the data itself (no database needed).
    dept_set = set(DEPARTMENTS)
    assert all(d[1] in dept_set for d in DOCTORS), "Doctor with unknown department"
    assert all(p[1] in dept_set for p in PROCEDURES), "Procedure with unknown department"
    print(f"{len(DEPARTMENTS)} departments, {len(ROOM_TYPES)} rooms, "
          f"{len(DOCTORS)} doctors, {len(PROCEDURES)} procedures, "
          f"{len(SETTINGS)} settings - data OK")

    # Worked example: TKR, Single Private Room, Dr. Sanjay Deshmukh
    p = next(x for x in PROCEDURES if x[0].startswith("Total Knee"))
    room = next(r for r in ROOM_TYPES if r[0] == "Single Private Room")[1]
    pct = next(d for d in DOCTORS if d[0] == "Dr. Sanjay Deshmukh")[3]
    stay = p[7]
    room_total = room * stay
    surgeon = p[2] * pct // 100
    subtotal = room_total + p[2] + p[3] + p[4] + p[5] + p[6] + surgeon
    contingency = subtotal * 10 // 100
    print(f"TKR example: room {room_total:,} + procedure {p[2]:,} + OT {p[3]:,} + "
          f"anaesthesia {p[4]:,} + consumables {p[5]:,} + investigations {p[6]:,} + "
          f"surgeon {surgeon:,} = subtotal {subtotal:,}; "
          f"+10% contingency = {subtotal + contingency:,}")

    # Test actual seeding into hospital.db
    c = get_connection("hospital.db")
    seed(c)
    print("Database hospital.db successfully seeded!")
    c.close()
