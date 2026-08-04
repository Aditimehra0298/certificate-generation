"""
Course name → certificate template mapping.

Templates live in templates/*.pdf (2 pages each: certificate + transcript).
Each PDF filename stem is the canonical course name shown on the certificate.

Incoming `courseName` values from n8n / LMS are resolved in this order:
  1. Explicit alias (COURSE_ALIASES)
  2. Exact normalized match against a template filename
  3. Fuzzy match (SequenceMatcher) with collision guard for similar courses
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Canonical template stems — auto-synced from templates/*.pdf at runtime,
# but listed here for documentation and alias targets.
CANONICAL_TEMPLATES: tuple[str, ...] = (
    "Advanced Food Fraud Mitigation and Auditing Aligned with FSSC 22000:2018 VERSION 6",
    "Business and Corporate Ethics : A Professional Starter Programme",
    "CARBON TRADING & REPORTING PRACTITIONER PROGRAM",
    "Carbon Trading & Reporting",
    "Climate Adaptation & Cost of Inaction Training Course",
    "Cyber Security Phishing Awareness Trainings",
    "DIPLOMA IN CYBERSECURITY & ETHICAL HACKING (FOUNDATIONS)",
    "DIPLOMA IN HACCP FOOD SAFETY STANDARDS (LEVEL 2)",
    "ESG Management Development Training Program",
    "EU Cybersecurity Compliance Core (NIS2 & DORA)",
    "Essentials of Carbon Trading & Reporting",
    "FSSC 22000 Category E PRP Implementation and Audit Training for Catering Operations",
    "Food Defense Training Aligned with FSSC 22000 : 2018 Requirements",
    "GDPR - EU Data Protection Foundation Course",
    "ISMS Induction Training  Program",
    "ISO 140012015 EMS Internal Auditor Course",
    "ISO 140012015 EMS Internal Auditor",
    "ISO 140012015 EMS Lead Auditor Course",
    "ISO 140012026 EMS Lead Auditor",
    "ISO 14001:2026  Lead Auditor Transition  Course",
    "ISO 14064 GHG LEAD VERIFIER COURSE",
    "ISO 14064 Mastering GHG Accounting & Verification",
    "ISO 14971:2019- Application of Risk Management to medical Devices",
    "ISO 190112018 – Guidelines for Auditing Management Systems",
    "ISO 22716 GMPs: Best Practices For Cosmetics",
    "ISOIEC 17021-1:2015 including guidance for internal auditing of FSMS by Certification Bodies",
    "ISOIEC 27001:2022 Awareness Course",
    "ITC India ISMS Induction Training Program",
    "Mastering EU MDR Essentials: Practical Compliance for Medical Devices",
    "PRP Requirements for Feed and Animal Food Production as per ISO-TS 22002-6:2016",
    "PRP Requirements for Transport and Storage as per ISO-TS 22002-5:2019",
    "Training on ISO 27001 Annex 8",
    "Understanding and Implementing POSH in the Workplace",
)

# Aliases map normalized lookup keys → canonical template stem (must match a PDF filename).
# Includes legacy filenames and common LMS / n8n courseName variants.
COURSE_ALIASES: dict[str, str] = {
    # --- Renamed / replaced templates (legacy filenames) ---
    "advanced food fraud mitigation and auditing fssc 220002018 version 6": (
        "Advanced Food Fraud Mitigation and Auditing Aligned with FSSC 22000:2018 VERSION 6"
    ),
    "advanced food fraud mitigation and auditing fssc 22000 2018 version 6": (
        "Advanced Food Fraud Mitigation and Auditing Aligned with FSSC 22000:2018 VERSION 6"
    ),
    "cybersecurity awareness phishness": "Cyber Security Phishing Awareness Trainings",
    "food defense training aligned with fssc 22000 2018 requirements": (
        "Food Defense Training Aligned with FSSC 22000 : 2018 Requirements"
    ),
    "iso 149712019 application of risk management to medical devices": (
        "ISO 14971:2019- Application of Risk Management to medical Devices"
    ),
    "isoiec 27001 2022 awareness course": "ISOIEC 27001:2022 Awareness Course",
    "mastering eu mdr essentials practical compliance for medical devices": (
        "Mastering EU MDR Essentials: Practical Compliance for Medical Devices"
    ),
    "prp requirements for feed and animal food production as per isots 22002 62016": (
        "PRP Requirements for Feed and Animal Food Production as per ISO-TS 22002-6:2016"
    ),
    "prp requirements for transport and storage as per isots 22002 52019": (
        "PRP Requirements for Transport and Storage as per ISO-TS 22002-5:2019"
    ),
    # --- Common short / alternate courseName values from workflows ---
    "haccp food safety standards level 2": "DIPLOMA IN HACCP FOOD SAFETY STANDARDS (LEVEL 2)",
    "diploma in haccp food safety standards level 2": "DIPLOMA IN HACCP FOOD SAFETY STANDARDS (LEVEL 2)",
    "diploma in cybersecurity ethical hacking foundations": (
        "DIPLOMA IN CYBERSECURITY & ETHICAL HACKING (FOUNDATIONS)"
    ),
    "iso iec 27001 2022 awareness course": "ISOIEC 27001:2022 Awareness Course",
    "iso 14001 2015 ems internal auditor": "ISO 140012015 EMS Internal Auditor",
    "iso 14001 2015 ems internal auditor course": "ISO 140012015 EMS Internal Auditor Course",
    "iso 14001 2015 ems lead auditor course": "ISO 140012015 EMS Lead Auditor Course",
    "iso 14001 2026 ems lead auditor": "ISO 140012026 EMS Lead Auditor",
    "iso 14001 2026 lead auditor transition course": "ISO 14001:2026  Lead Auditor Transition  Course",
    "isms induction training program": "ISMS Induction Training  Program",
    "carbon trading reporting practitioner program": "CARBON TRADING & REPORTING PRACTITIONER PROGRAM",
    "iso 19011 2018 guidelines for auditing management systems": (
        "ISO 190112018 – Guidelines for Auditing Management Systems"
    ),
    "posh in the workplace": "Understanding and Implementing POSH in the Workplace",
    "understanding and implementing posh": "Understanding and Implementing POSH in the Workplace",
}


def normalize_course_name(value: str) -> str:
    """Normalize course/template names for lookup and fuzzy matching."""
    text = value.lower().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def list_pdf_templates(templates_dir: Path = TEMPLATES_DIR) -> list[Path]:
    """Return sorted PDF template paths from the templates directory."""
    return sorted(templates_dir.glob("*.pdf"))


def validate_template_registry(templates_dir: Path = TEMPLATES_DIR) -> list[str]:
    """Return warning messages when registry and templates/ are out of sync."""
    warnings: list[str] = []
    on_disk = {p.stem for p in list_pdf_templates(templates_dir)}
    canonical = set(CANONICAL_TEMPLATES)

    missing = canonical - on_disk
    extra = on_disk - canonical

    for stem in sorted(missing):
        warnings.append(f"Registry lists template not on disk: {stem!r}")
    for stem in sorted(extra):
        warnings.append(f"Template on disk missing from registry: {stem!r}")

    for alias_stem in set(COURSE_ALIASES.values()):
        if alias_stem not in on_disk:
            warnings.append(f"Alias target missing on disk: {alias_stem!r}")

    return warnings


def list_available_courses(templates_dir: Path = TEMPLATES_DIR) -> list[dict[str, str]]:
    """Return metadata for every template PDF (for API listing / n8n reference)."""
    courses: list[dict[str, str]] = []
    for index, path in enumerate(list_pdf_templates(templates_dir), start=1):
        courses.append(
            {
                "index": str(index),
                "courseName": path.stem,
                "templateFile": path.name,
            }
        )
    return courses


def _stem_to_path(stem: str, templates: list[Path]) -> Path | None:
    for path in templates:
        if path.stem == stem:
            return path
    return None


def resolve_template_pdf(
    course_name: str,
    *,
    template_file: str | None = None,
    templates_dir: Path = TEMPLATES_DIR,
) -> Path:
    """
    Pick the certificate PDF for the given courseName (or explicit templateFile).
    Raises FileNotFoundError when no suitable template exists.
    """
    templates = list_pdf_templates(templates_dir)
    if not templates:
        raise FileNotFoundError(f"No PDF templates found in {templates_dir}")

    if template_file:
        explicit = templates_dir / template_file
        if not explicit.is_file():
            raise FileNotFoundError(f"templateFile not found: {template_file}")
        if explicit.suffix.lower() != ".pdf":
            raise FileNotFoundError(f"templateFile must be a .pdf: {template_file}")
        return explicit

    course_name = course_name.strip()
    if not course_name:
        raise FileNotFoundError("courseName is empty")

    norm_course = normalize_course_name(course_name)

    # 1) Explicit alias table
    alias_stem = COURSE_ALIASES.get(norm_course)
    if alias_stem:
        matched = _stem_to_path(alias_stem, templates)
        if matched:
            return matched

    # 2) Exact normalized filename match
    for path in templates:
        if normalize_course_name(path.stem) == norm_course:
            return path

    # 3) Fuzzy match with guard against near-tie collisions
    scored: list[tuple[float, Path]] = []
    for path in templates:
        norm_file = normalize_course_name(path.stem)
        ratio = SequenceMatcher(None, norm_course, norm_file).ratio()
        if norm_course in norm_file or norm_file in norm_course:
            ratio = max(ratio, 0.85)
        scored.append((ratio, path))

    scored.sort(key=lambda item: item[0], reverse=True)
    best_ratio, best_path = scored[0]
    second_ratio = scored[1][0] if len(scored) > 1 else 0.0

    if best_ratio < 0.55:
        available = ", ".join(p.stem for p in templates)
        raise FileNotFoundError(
            f"No certificate template matched courseName={course_name!r}. "
            f"Available templates: {available}"
        )

    if second_ratio >= 0.55 and (best_ratio - second_ratio) < 0.03:
        candidates = [p.stem for _, p in scored[:3] if _ >= 0.55]
        raise FileNotFoundError(
            f"Ambiguous courseName={course_name!r}; multiple templates match: "
            f"{', '.join(candidates)}. "
            f"Use templateFile with the exact PDF filename, or fix courseName."
        )

    return best_path
