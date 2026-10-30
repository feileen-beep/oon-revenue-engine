"""Reference data shared by the generator, parser and reports."""

LEVELS_OF_CARE = ["RTC", "PHP", "IOP", "OP", "TMS"]

LOC_NAMES = {
    "RTC": "Residential",
    "PHP": "Partial hospitalization",
    "IOP": "Intensive outpatient",
    "OP": "Outpatient",
    "TMS": "TMS therapy",
}

# Billing code used on the 835 service line (SVC01) for each level of care.
LOC_SERVICE_CODE = {
    "RTC": ("NU", "1002"),   # revenue code: residential, chemical dependency
    "PHP": ("NU", "0913"),   # revenue code: partial hospitalization, intensive
    "IOP": ("NU", "0906"),   # revenue code: intensive outpatient, chemical dependency
    "OP": ("HC", "90837"),   # CPT: psychotherapy, 60 minutes
    "TMS": ("HC", "90868"),  # CPT: TMS subsequent delivery and management
}

SITES = ["Glendale", "Scottsdale", "Phoenix"]

# Claim adjustment reason codes (CARC) the engine knows how to interpret.
CARC = {
    "1": "Deductible",
    "2": "Coinsurance",
    "3": "Copay",
    "16": "Missing or invalid information",
    "27": "Coverage terminated before service",
    "29": "Timely filing limit expired",
    "45": "Charge exceeds fee schedule / allowed amount",
    "50": "Not medically necessary",
    "96": "Non-covered charge",
    "100": "Payment made to patient / insured",
    "197": "Precertification / authorization absent",
}

DENIAL_CARCS = {"16", "27", "29", "50", "96", "197"}
PAID_TO_PATIENT_CARC = "100"

# Age buckets in days since the end of the service period.
AGE_BUCKETS = [(0, 30), (31, 60), (61, 90), (91, 180), (181, 365)]


def age_bucket(days: int) -> str:
    for lo, hi in AGE_BUCKETS:
        if days <= hi:
            return f"{lo}-{hi}"
    return "365+"


def bucket_start(label: str) -> int:
    return 366 if label == "365+" else int(label.split("-")[0])


BUCKET_LABELS = [f"{lo}-{hi}" for lo, hi in AGE_BUCKETS]
