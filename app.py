"""
Certificate PDF Generation API

Flask service that generates multi-page certificate PDFs from per-course PDF
templates (page 1 = certificate, page 2 = transcript), overlays dynamic fields,
and serves generated files. Designed for n8n workflows and deployment on
Render / Railway via gunicorn.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image as PILImage
from PIL import ImageDraw, ImageOps

from template_registry import (
    list_available_courses,
    list_pdf_templates,
    resolve_template_pdf,
    validate_template_registry,
)

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
GENERATED_DIR = BASE_DIR / "generated"
FONTS_DIR = BASE_DIR / "fonts"
ASSETS_DIR = BASE_DIR / "assets"
PLUMBING_QR_DEFAULT = ASSETS_DIR / "plumbing-qr.png"

FONT_REGULAR = FONTS_DIR / "times.ttf"
FONT_BOLD = FONTS_DIR / "timesbd.ttf"
FONT_FALLBACK_REGULAR = FONTS_DIR / "arial.ttf"
FONT_FALLBACK_BOLD = FONTS_DIR / "arialbd.ttf"
FONT_CORSIVA_CANDIDATES = (
    FONTS_DIR / "monotype-corsiva.ttf",
    FONTS_DIR / "Monotype Corsiva.ttf",
    FONTS_DIR / "MTCORSVA.TTF",
)
FONT_GREAT_VIBES = FONTS_DIR / "GreatVibes-Regular.ttf"
FONT_ALLURA = FONTS_DIR / "Allura-Regular.ttf"
FONT_MONTSERRAT_MEDIUM = FONTS_DIR / "Montserrat-Medium.ttf"
FONT_MONTSERRAT_SEMIBOLD = FONTS_DIR / "Montserrat-SemiBold.ttf"

# Template PDF page size in points (matches templates/*.pdf mediabox)
PAGE_WIDTH = 1860.0
PAGE_HEIGHT = 2631.0
PAGE_SIZE = (PAGE_WIDTH, PAGE_HEIGHT)

# Source template pixel dimensions (for coordinate conversion)
TEMPLATE_WIDTH_PX = 2480
TEMPLATE_HEIGHT_PX = 3508

# Layout offsets/font sizes were tuned on portrait A4 (~595 pt wide); scale for PDF templates.
_PT_SCALE = PAGE_WIDTH / 595.27

# Writable output directory:
# - local/Render/Railway: project ./generated
# - Vercel serverless: /tmp/generated (read-only app bundle at /var/task)
OUTPUT_DIR = Path("/tmp/generated") if os.environ.get("VERCEL") else GENERATED_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Public base URL for pdfUrl in responses (set via env on Render/Railway)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# ---------------------------------------------------------------------------
# Text positioning — calibrated to template page 1 & page 2 designs.
# Use _px(x_from_left, y_from_top) to convert template pixels → PDF points.
# ---------------------------------------------------------------------------


def _px(x_px: float, y_px_from_top: float) -> tuple[float, float]:
    """Map template pixel coordinates to ReportLab points (origin bottom-left)."""
    x_pt = x_px / TEMPLATE_WIDTH_PX * PAGE_WIDTH
    y_pt = PAGE_HEIGHT - (y_px_from_top / TEMPLATE_HEIGHT_PX * PAGE_HEIGHT)
    return x_pt, y_pt


class Page1Layout:
    """Certificate of Attainment — field positions (course title is on the template)."""

    # Baseline for recipient name (px from top/left on template)
    CANDIDATE_NAME_BASELINE_Y_PX = 1185
    CANDIDATE_NAME_X_PX = 1315
    CANDIDATE_NAME = (
        _px(CANDIDATE_NAME_X_PX, CANDIDATE_NAME_BASELINE_Y_PX)[0] + 34,
        _px(CANDIDATE_NAME_X_PX, CANDIDATE_NAME_BASELINE_Y_PX)[1] - 30,
    )
    CANDIDATE_FONT_SIZE = 44 * _PT_SCALE

    # Bottom-left metadata values — shared X so all values align after labels
    META_VALUE_X = _px(870, 0)[0] + 25
    META_FONT_SIZE = 11 * _PT_SCALE
    ISSUE_DATE_Y = _px(0, 2699)[1] - 5
    CERT_NUMBER_Y = _px(0, 2769)[1] - 4 * _PT_SCALE - 5
    DELEGATE_NUMBER_Y = _px(0, 2886)[1] - 5

    # Verification QR (top-left placeholder on template)
    QR_SIZE = 68 * _PT_SCALE
    QR_TOP_LEFT = _px(72, 72)


class PlumbingLayout:
    """Single-page landscape euroTECH plumbing certificate."""

    PAGE_WIDTH = 1086.0
    PAGE_HEIGHT = 814.5
    # Spec: name #0B2161, other variable text #111111
    NAVY = (0x0B / 255, 0x21 / 255, 0x61 / 255)
    BLACK = (0x11 / 255, 0x11 / 255, 0x11 / 255)

    # Name: between "THIS IS TO CERTIFY THAT" (~267) and gold line (~331.5)
    NAME_CENTER_X = 576.5
    NAME_BASELINE_FROM_TOP = 308.0
    NAME_FONT_SIZE = 46.0

    # UID value only — same baseline as printed "UID:" (~349–361), slightly larger
    UID_X = 513.0
    UID_BASELINE_FROM_TOP = 353.6
    UID_FONT_SIZE = 18.0

    # Footer values sit on the blank lines under each label
    FOOTER_BASELINE_FROM_TOP = 716.0
    FOOTER_FONT_SIZE = 9.5
    DURATION_FONT_SIZE = 11.0
    CERT_NUMBER_CENTER_X = 312.0   # shifted slightly right under "CERTIFICATE NO."
    ISSUE_DATE_CENTER_X = 460.5    # underline 434–487
    START_DATE_CENTER_X = 700.0    # underline 652–748
    DURATION_CENTER_X = 854.5      # underline 832–877, before MONTHS
    DURATION_BASELINE_FROM_TOP = 716.0
    CERT_NUMBER_BASELINE_FROM_TOP = 716.0
    # Template has no cert-no underline; draw one on the same row as issue/start/duration.
    # 720.5 (not 728) compensates for the template cropbox sitting 7.5pt above mediabox origin.
    CERT_UNDERLINE_FROM_TOP = 720.5
    CERT_UNDERLINE_STROKE = 1.6

    # Scan-to-verify inner area (orange box ~506–583 × 673–748)
    QR_SIZE = 58.0
    QR_CENTER_X = 544.5
    QR_CENTER_FROM_TOP = 702.6

    # Candidate photo fills the inner rounded frame (orange outer ~880–1020 × 188–379).
    # PHOTO_TOP is 7.5pt less than visual Y because overlay merge sits on the cropbox.
    PHOTO_X = 883.0
    PHOTO_WIDTH = 134.0
    PHOTO_TOP = 183.5
    PHOTO_HEIGHT = 187.0
    PHOTO_RADIUS = 12.0

    # After "The performance of the candidate has been found"
    GRADE_X = 728.0
    GRADE_BASELINE_FROM_TOP = 650.0
    GRADE_FONT_SIZE = 11.0


class Page2Layout:
    """Program Transcript — field positions (training program is on the template)."""

    # “Name :” row (y≈760) — total shift: right 86pt, down 32.5pt (A4-calibrated)
    NAME = (
        _px(362, 760)[0] + 86 * _PT_SCALE + 10,
        _px(362, 760)[1] - 32.5 * _PT_SCALE,
    )
    NAME_FONT_SIZE = 11 * _PT_SCALE

    # “Overall Grade:” row (y≈850) — separate line from name
    GRADE = (_px(2276, 850)[0] + 30, _px(2276, 850)[1] - 3.4 * _PT_SCALE - 5)
    GRADE_FONT_SIZE = 11 * _PT_SCALE

    # Centered in the blank band above “Certificate Number” / “Issue Date” labels
    FOOTER_CERT_NUMBER = (
        _px(310, 3210)[0] + 92 * _PT_SCALE,
        _px(310, 3210)[1] + 45 * _PT_SCALE + 10,
    )
    FOOTER_ISSUE_DATE = (
        _px(1240, 3210)[0] + 30 * _PT_SCALE,
        _px(1240, 3210)[1] + 48 * _PT_SCALE + 2,
    )
    FOOTER_FONT_SIZE = 11 * _PT_SCALE


# Required JSON fields for certificate generation
REQUIRED_FIELDS = (
    "certificateId",
    "candidateName",
    "courseName",
    "grade",
    "certificateNumber",
    "delegateNumber",
    "verifyUrl",
)

# Filename safety pattern for served PDFs
SAFE_FILENAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.pdf$", re.I)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    """Application factory for production WSGI servers."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB JSON payload limit
    CORS(app, resources={r"/*": {"origins": os.environ.get("CORS_ORIGINS", "*")}})

    _register_fonts()
    _validate_startup_assets()

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"}), 200

    @app.route("/templates", methods=["GET"])
    def list_templates():
        return jsonify({"success": True, "templates": list_available_courses()}), 200

    @app.route("/generate-certificate", methods=["POST"])
    def generate_certificate():
        return _handle_generate_certificate()

    @app.route("/generated/<path:filename>", methods=["GET"])
    def serve_generated(filename: str):
        return _handle_serve_generated(filename)

    return app


# ---------------------------------------------------------------------------
# Font registration
# ---------------------------------------------------------------------------

_fonts_registered = False
_corsiva_font_path: Path | None = None


def _find_corsiva_font() -> Path | None:
    for path in FONT_CORSIVA_CANDIDATES:
        if path.is_file():
            return path
    return None


def _register_fonts() -> None:
    """Register certificate fonts including Monotype Corsiva for recipient names."""
    global _fonts_registered, _corsiva_font_path
    if _fonts_registered:
        return

    regular = FONT_REGULAR if FONT_REGULAR.exists() else FONT_FALLBACK_REGULAR
    bold = FONT_BOLD if FONT_BOLD.exists() else FONT_FALLBACK_BOLD

    if regular.exists():
        pdfmetrics.registerFont(TTFont("CertTimes", str(regular)))
        pdfmetrics.registerFontFamily(
            "CertTimes",
            normal="CertTimes",
            bold="CertTimes-Bold" if bold.exists() else "CertTimes",
        )
    else:
        logger.warning("No serif font found in %s; using Helvetica", FONTS_DIR)

    if bold.exists():
        pdfmetrics.registerFont(TTFont("CertTimes-Bold", str(bold)))

    _corsiva_font_path = _find_corsiva_font()
    if _corsiva_font_path:
        pdfmetrics.registerFont(TTFont("CertCorsiva", str(_corsiva_font_path)))
    else:
        logger.warning(
            "Monotype Corsiva not found in %s; recipient name will use Times. "
            "Add monotype-corsiva.ttf to fonts/.",
            FONTS_DIR,
        )

    if FONT_GREAT_VIBES.is_file():
        pdfmetrics.registerFont(TTFont("GreatVibes", str(FONT_GREAT_VIBES)))
    elif FONT_ALLURA.is_file():
        pdfmetrics.registerFont(TTFont("GreatVibes", str(FONT_ALLURA)))
        logger.warning("Great Vibes missing; using Allura for plumbing names")
    if FONT_MONTSERRAT_MEDIUM.is_file():
        pdfmetrics.registerFont(TTFont("Montserrat-Medium", str(FONT_MONTSERRAT_MEDIUM)))
    if FONT_MONTSERRAT_SEMIBOLD.is_file():
        pdfmetrics.registerFont(TTFont("Montserrat-SemiBold", str(FONT_MONTSERRAT_SEMIBOLD)))

    _fonts_registered = True


def _font_name(bold: bool = False) -> str:
    """Return registered certificate serif font."""
    bold_path = FONT_BOLD if FONT_BOLD.exists() else FONT_FALLBACK_BOLD
    regular_path = FONT_REGULAR if FONT_REGULAR.exists() else FONT_FALLBACK_REGULAR
    if bold and bold_path.exists():
        return "CertTimes-Bold"
    if regular_path.exists():
        return "CertTimes"
    return "Helvetica-Bold" if bold else "Helvetica"


def _name_font_name() -> str:
    """Return Monotype Corsiva when available, otherwise the regular certificate font."""
    if _corsiva_font_path and _corsiva_font_path.is_file():
        return "CertCorsiva"
    return _font_name(bold=False)


# ---------------------------------------------------------------------------
# Validation & helpers
# ---------------------------------------------------------------------------

def _validate_startup_assets() -> None:
    """Log warnings if templates or fonts are missing (fail on generate)."""
    templates = list_pdf_templates()
    if not templates:
        logger.warning("No PDF templates found in %s", TEMPLATES_DIR)
    else:
        logger.info("Loaded %d certificate templates from %s", len(templates), TEMPLATES_DIR)
    for message in validate_template_registry(TEMPLATES_DIR):
        logger.warning(message)
    if not FONT_REGULAR.is_file():
        logger.warning("Missing font: %s", FONT_REGULAR)


def _template_page_size(template_reader: PdfReader, page_index: int) -> tuple[float, float]:
    """Return (width, height) in points for a template page."""
    page = template_reader.pages[page_index]
    return float(page.mediabox.width), float(page.mediabox.height)


def _validate_payload(data: dict[str, Any]) -> tuple[dict[str, str] | None, tuple[dict, int] | None]:
    """
    Validate incoming JSON. Returns (cleaned_fields, None) or (None, error_response).
    """
    if not isinstance(data, dict):
        return None, (jsonify({"success": False, "error": "Request body must be a JSON object"}), 400)

    missing = [f for f in REQUIRED_FIELDS if not _non_empty_str(data.get(f))]
    if missing:
        return None, (
            jsonify({"success": False, "error": f"Missing or empty required fields: {', '.join(missing)}"}),
            400,
        )

    verify_url = str(data["verifyUrl"]).strip()
    if not verify_url.startswith(("http://", "https://")):
        return None, (jsonify({"success": False, "error": "verifyUrl must be a valid http(s) URL"}), 400)

    issue_date = data.get("issueDate")
    if issue_date is not None and not _non_empty_str(issue_date):
        issue_date = None

    template_file = data.get("templateFile")
    if template_file is not None:
        template_file = str(template_file).strip() or None

    cleaned = {
        "certificateId": str(data["certificateId"]).strip(),
        "candidateName": str(data["candidateName"]).strip(),
        "courseName": str(data["courseName"]).strip(),
        "grade": str(data["grade"]).strip(),
        "certificateNumber": str(data["certificateNumber"]).strip(),
        "delegateNumber": str(data["delegateNumber"]).strip(),
        "verifyUrl": verify_url,
        "issueDate": str(issue_date).strip() if issue_date else _default_issue_date(),
        "templateFile": template_file,
        "uid": str(data.get("uid") or data["delegateNumber"]).strip(),
        "startDate": str(data.get("startDate") or data.get("trainingStartDate") or "").strip(),
        "trainingDuration": str(data.get("trainingDuration") or data.get("duration") or "").strip(),
        "qrPath": str(data.get("qrPath") or "").strip(),
        "photoPath": str(data.get("photoPath") or "").strip(),
        "qrUrl": str(data.get("qrUrl") or "").strip(),
        "photoUrl": str(data.get("photoUrl") or "").strip(),
        "qrBase64": str(data.get("qrBase64") or "").strip(),
        "photoBase64": str(data.get("photoBase64") or "").strip(),
    }
    return cleaned, None


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _default_issue_date() -> str:
    """ISO-style display date when issueDate is omitted."""
    return datetime.now(timezone.utc).strftime("%d %B %Y")


def _public_pdf_url(filename: str) -> str:
    """Build absolute pdfUrl for API response."""
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/generated/{filename}"
    # Relative URL for local dev when PUBLIC_BASE_URL is unset
    return f"/generated/{filename}"


def _draw_centered_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    font_size: float,
    bold: bool = False,
    color: tuple[float, float, float] = (0.08, 0.08, 0.08),
    font: str | None = None,
) -> None:
    """Draw horizontally centered text at (x, y)."""
    font = font or _font_name(bold=bold)
    c.setFont(font, font_size)
    c.setFillColorRGB(*color)
    text_width = c.stringWidth(text, font, font_size)
    c.drawString(x - text_width / 2, y, text)


def _draw_centered_text_in_gap(
    c: canvas.Canvas,
    text: str,
    x_px: float,
    y_center_px: float,
    font_size: float,
    bold: bool = False,
    color: tuple[float, float, float] = (0.08, 0.08, 0.08),
) -> None:
    """Center text vertically in a template gap (y measured from top of image)."""
    font = _font_name(bold=bold)
    ascent = pdfmetrics.getAscent(font) / 1000.0 * font_size
    descent = abs(pdfmetrics.getDescent(font) / 1000.0 * font_size)
    x_pt, y_center_pt = _px(x_px, y_center_px)
    baseline_pt = y_center_pt - (ascent - descent) / 2
    _draw_centered_text(c, text, x_pt, baseline_pt, font_size, bold, color)


def _draw_left_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    font_size: float,
    bold: bool = False,
    color: tuple[float, float, float] = (0.08, 0.08, 0.08),
    font: str | None = None,
) -> None:
    """Draw left-aligned text with baseline at (x, y)."""
    font = font or _font_name(bold=bold)
    c.setFont(font, font_size)
    c.setFillColorRGB(*color)
    c.drawString(x, y, text)


def _draw_right_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    font_size: float,
    bold: bool = False,
    color: tuple[float, float, float] = (0.08, 0.08, 0.08),
) -> None:
    """Draw right-aligned text ending at x."""
    font = _font_name(bold=bold)
    c.setFont(font, font_size)
    c.setFillColorRGB(*color)
    text_width = c.stringWidth(text, font, font_size)
    c.drawString(x - text_width, y, text)


def _wrap_text_lines(
    text: str,
    font_name: str,
    font_size: float,
    max_width: float,
) -> list[str]:
    """Wrap text to fit within max_width (points)."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if pdfmetrics.stringWidth(trial, font_name, font_size) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _draw_wrapped_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    font_size: float,
    max_width: float,
    line_spacing: float = 1.25,
    bold: bool = False,
    align: str = "left",
    color: tuple[float, float, float] = (0.08, 0.08, 0.08),
) -> None:
    """Draw wrapped text; y is the baseline of the first line."""
    font = _font_name(bold=bold)
    lines = _wrap_text_lines(text, font, font_size, max_width)
    leading = font_size * line_spacing
    c.setFont(font, font_size)
    c.setFillColorRGB(*color)
    for i, line in enumerate(lines):
        line_y = y - i * leading
        if align == "center":
            width = c.stringWidth(line, font, font_size)
            c.drawString(x - width / 2, line_y, line)
        elif align == "right":
            width = c.stringWidth(line, font, font_size)
            c.drawString(x - width, line_y, line)
        else:
            c.drawString(x, line_y, line)


def _draw_rect_from_template_px(
    c: canvas.Canvas,
    x1: int,
    y1_top: int,
    x2: int,
    y2_top: int,
    fill_rgb: tuple[float, float, float],
) -> None:
    """Fill a rectangle defined by template pixel coords (y measured from top)."""
    left, top_y = _px(x1, y1_top)
    right, bottom_y = _px(x2, y2_top)
    c.setFillColorRGB(*fill_rgb)
    c.rect(left, bottom_y, right - left, top_y - bottom_y, stroke=0, fill=1)


def _trim_qr_quiet_zone(img: PILImage.Image, margin: int = 8) -> PILImage.Image:
    """Crop extra white padding so the QR fills the scan box."""
    gray = img.convert("L")
    w, h = gray.size
    px = gray.load()
    ink_x: list[int] = []
    ink_y: list[int] = []
    for y in range(h):
        for x in range(w):
            if px[x, y] < 240:
                ink_x.append(x)
                ink_y.append(y)
    if not ink_x:
        return img
    left = max(0, min(ink_x) - margin)
    top = max(0, min(ink_y) - margin)
    right = min(w, max(ink_x) + 1 + margin)
    bottom = min(h, max(ink_y) + 1 + margin)
    return img.crop((left, top, right, bottom))


def _cover_crop_image(path: Path, target_w: float, target_h: float) -> ImageReader:
    """Crop an image to fill target_w x target_h without stretching (object-fit: cover)."""
    img = PILImage.open(path)
    return _cover_crop_pil(img, target_w, target_h)


def _prepare_student_photo(img: PILImage.Image) -> PILImage.Image:
    """Normalize any uploaded student photo (orientation, transparency, mode)."""
    img = ImageOps.exif_transpose(img) or img
    if img.mode in ("RGBA", "LA"):
        background = PILImage.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    if img.mode == "P":
        img = img.convert("RGBA")
        background = PILImage.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")


def _cover_crop_pil(img: PILImage.Image, target_w: float, target_h: float) -> ImageReader:
    """Cover-crop any student photo into the rounded frame (object-fit: cover)."""
    img = _prepare_student_photo(img)
    src_w, src_h = img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        new_w = max(1, int(round(src_h * target_ratio)))
        left = max(0, (src_w - new_w) // 2)
        img = img.crop((left, 0, left + new_w, src_h))
    elif src_ratio < target_ratio:
        new_h = max(1, int(round(src_w / target_ratio)))
        extra = max(0, src_h - new_h)
        top = max(0, int(round(extra * 0.22)))
        if top + new_h > src_h:
            top = src_h - new_h
        img = img.crop((0, top, src_w, top + new_h))

    scale = 4
    out_w = max(1, int(round(target_w * scale)))
    out_h = max(1, int(round(target_h * scale)))
    radius = max(1, int(round(PlumbingLayout.PHOTO_RADIUS * scale)))
    img = img.resize((out_w, out_h), PILImage.Resampling.LANCZOS).convert("RGBA")
    mask = PILImage.new("L", (out_w, out_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, out_w - 1, out_h - 1), radius=radius, fill=255)
    rounded = PILImage.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    rounded.paste(img, (0, 0), mask)
    buffer = io.BytesIO()
    rounded.save(buffer, format="PNG")
    buffer.seek(0)
    return ImageReader(buffer)


def _decode_base64_image(value: str) -> PILImage.Image:
    """Decode a base64 string (optionally data-URL prefixed) into a PIL image."""
    payload = value.strip()
    if "," in payload and payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[1]
    raw = base64.b64decode(payload)
    return PILImage.open(io.BytesIO(raw))


def _fetch_image_from_url(url: str) -> PILImage.Image:
    """Download an image from a public http(s) URL."""
    req = Request(url, headers={"User-Agent": "certificate-generation-api/1.0"})
    with urlopen(req, timeout=20) as response:
        data = response.read()
    return PILImage.open(io.BytesIO(data))


def _resolve_plumbing_image(
    fields: dict[str, str],
    *,
    path_key: str,
    url_key: str,
    b64_key: str,
    default_path: Path | None,
    kind: str,
) -> ImageReader | None:
    """
    Resolve plumbing overlay image from the request.
    Priority: base64 > URL > path > default (only if a default is provided).
    Student photos have no default — each request must send its own image.
    """
    def _to_reader(img: PILImage.Image) -> ImageReader:
        if kind == "photo":
            return _cover_crop_pil(img, PlumbingLayout.PHOTO_WIDTH, PlumbingLayout.PHOTO_HEIGHT)
        if kind == "qr":
            img = _trim_qr_quiet_zone(img)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        return ImageReader(buffer)

    b64_value = fields.get(b64_key, "")
    if b64_value:
        try:
            return _to_reader(_decode_base64_image(b64_value))
        except Exception as exc:
            logger.warning("Failed to decode %s: %s", b64_key, exc)

    url_value = fields.get(url_key, "")
    if url_value:
        if not url_value.startswith(("http://", "https://")):
            raise ValueError(f"{url_key} must be a valid http(s) URL")
        try:
            return _to_reader(_fetch_image_from_url(url_value))
        except (URLError, OSError, ValueError) as exc:
            logger.warning("Failed to fetch %s=%r: %s", url_key, url_value, exc)

    path_value = fields.get(path_key, "")
    if path_value:
        path = Path(path_value)
        if path.is_file():
            if kind == "photo":
                return _cover_crop_image(path, PlumbingLayout.PHOTO_WIDTH, PlumbingLayout.PHOTO_HEIGHT)
            return _to_reader(PILImage.open(path).convert("RGBA"))

    if default_path is not None and default_path.is_file():
        if kind == "photo":
            return _cover_crop_image(default_path, PlumbingLayout.PHOTO_WIDTH, PlumbingLayout.PHOTO_HEIGHT)
        return _to_reader(PILImage.open(default_path).convert("RGBA"))
    return None


def _from_top(page_height: float, y_from_top: float) -> float:
    """Convert y measured from the top of the page to ReportLab baseline y."""
    return page_height - y_from_top


def _is_plumbing_template(template_path: Path) -> bool:
    stem = template_path.stem.lower()
    return "plumbing" in stem


def _plumbing_name_font() -> str:
    if "GreatVibes" in pdfmetrics.getRegisteredFontNames():
        return "GreatVibes"
    return _name_font_name()


def _plumbing_medium_font() -> str:
    if "Montserrat-Medium" in pdfmetrics.getRegisteredFontNames():
        return "Montserrat-Medium"
    return _font_name(bold=False)


def _plumbing_semibold_font() -> str:
    if "Montserrat-SemiBold" in pdfmetrics.getRegisteredFontNames():
        return "Montserrat-SemiBold"
    return _font_name(bold=True)


def _draw_plumbing_overlay(c: canvas.Canvas, fields: dict[str, str]) -> None:
    """Draw name, UID, and footer fields on the landscape plumbing certificate."""
    layout = PlumbingLayout
    name_y = _from_top(layout.PAGE_HEIGHT, layout.NAME_BASELINE_FROM_TOP)
    _draw_centered_text(
        c,
        fields["candidateName"],
        layout.NAME_CENTER_X,
        name_y,
        layout.NAME_FONT_SIZE,
        color=layout.NAVY,
        font=_plumbing_name_font(),
    )

    uid = fields.get("uid") or fields.get("delegateNumber") or ""
    if uid:
        _draw_left_text(
            c,
            uid,
            layout.UID_X,
            _from_top(layout.PAGE_HEIGHT, layout.UID_BASELINE_FROM_TOP),
            layout.UID_FONT_SIZE,
            color=layout.BLACK,
            font=_plumbing_medium_font(),
        )

    footer_y = _from_top(layout.PAGE_HEIGHT, layout.FOOTER_BASELINE_FROM_TOP)
    footer_size = layout.FOOTER_FONT_SIZE
    medium = _plumbing_medium_font()
    cert_number = fields["certificateNumber"]
    _draw_centered_text(
        c,
        cert_number,
        layout.CERT_NUMBER_CENTER_X,
        _from_top(layout.PAGE_HEIGHT, layout.CERT_NUMBER_BASELINE_FROM_TOP),
        footer_size,
        color=layout.BLACK,
        font=medium,
    )
    cert_width = c.stringWidth(cert_number, medium, footer_size)
    underline_y = _from_top(layout.PAGE_HEIGHT, layout.CERT_UNDERLINE_FROM_TOP)
    c.setStrokeColorRGB(*layout.BLACK)
    c.setLineWidth(layout.CERT_UNDERLINE_STROKE)
    c.line(
        layout.CERT_NUMBER_CENTER_X - cert_width / 2,
        underline_y,
        layout.CERT_NUMBER_CENTER_X + cert_width / 2,
        underline_y,
    )
    _draw_centered_text(
        c,
        fields["issueDate"],
        layout.ISSUE_DATE_CENTER_X,
        footer_y,
        footer_size,
        color=layout.BLACK,
        font=medium,
    )
    start_date = fields.get("startDate") or fields.get("trainingStartDate") or ""
    if start_date:
        _draw_centered_text(
            c,
            start_date,
            layout.START_DATE_CENTER_X,
            footer_y,
            footer_size,
            color=layout.BLACK,
            font=medium,
        )
    duration = fields.get("trainingDuration") or fields.get("duration") or ""
    if duration:
        duration_value = duration.strip()
        for suffix in (" months", " month", "Months", "MONTHS"):
            if duration_value.lower().endswith(suffix.lower()):
                duration_value = duration_value[: -len(suffix)].strip()
                break
        _draw_centered_text(
            c,
            duration_value,
            layout.DURATION_CENTER_X,
            _from_top(layout.PAGE_HEIGHT, layout.DURATION_BASELINE_FROM_TOP),
            layout.DURATION_FONT_SIZE,
            color=layout.BLACK,
            font=_plumbing_semibold_font(),
        )

    grade = (fields.get("grade") or "").strip()
    if grade:
        grade_text = grade if grade.endswith(".") else f"{grade}."
        _draw_left_text(
            c,
            grade_text,
            layout.GRADE_X,
            _from_top(layout.PAGE_HEIGHT, layout.GRADE_BASELINE_FROM_TOP),
            layout.GRADE_FONT_SIZE,
            color=layout.BLACK,
            font="Helvetica-Bold",
        )

    qr_image = _resolve_plumbing_image(
        fields,
        path_key="qrPath",
        url_key="qrUrl",
        b64_key="qrBase64",
        default_path=PLUMBING_QR_DEFAULT,
        kind="qr",
    )
    if qr_image:
        size = layout.QR_SIZE
        qr_x = layout.QR_CENTER_X - size / 2
        qr_y = _from_top(layout.PAGE_HEIGHT, layout.QR_CENTER_FROM_TOP) - size / 2
        c.drawImage(
            qr_image,
            qr_x,
            qr_y,
            width=size,
            height=size,
            preserveAspectRatio=True,
            mask="auto",
        )

    photo_image = _resolve_plumbing_image(
        fields,
        path_key="photoPath",
        url_key="photoUrl",
        b64_key="photoBase64",
        default_path=None,
        kind="photo",
    )
    if photo_image:
        photo_x = layout.PHOTO_X
        photo_w = layout.PHOTO_WIDTH
        photo_h = layout.PHOTO_HEIGHT
        photo_y = _from_top(layout.PAGE_HEIGHT, layout.PHOTO_TOP) - photo_h
        c.drawImage(
            photo_image,
            photo_x,
            photo_y,
            width=photo_w,
            height=photo_h,
            preserveAspectRatio=False,
            mask="auto",
        )


def _draw_page1_overlay(c: canvas.Canvas, fields: dict[str, str]) -> None:
    """Draw dynamic fields on certificate page 1."""
    _draw_centered_text(
        c,
        fields["candidateName"],
        Page1Layout.CANDIDATE_NAME[0],
        Page1Layout.CANDIDATE_NAME[1],
        Page1Layout.CANDIDATE_FONT_SIZE,
        bold=True,
    )

    meta_x = Page1Layout.META_VALUE_X
    meta_size = Page1Layout.META_FONT_SIZE
    _draw_left_text(c, fields["issueDate"], meta_x, Page1Layout.ISSUE_DATE_Y, meta_size, bold=True)
    _draw_left_text(c, fields["certificateNumber"], meta_x, Page1Layout.CERT_NUMBER_Y, meta_size, bold=True)
    _draw_left_text(c, fields["delegateNumber"], meta_x, Page1Layout.DELEGATE_NUMBER_Y, meta_size, bold=True)


def _draw_page2_overlay(c: canvas.Canvas, fields: dict[str, str]) -> None:
    """Draw dynamic fields on transcript page 2."""
    _draw_left_text(
        c,
        fields["candidateName"],
        Page2Layout.NAME[0],
        Page2Layout.NAME[1],
        Page2Layout.NAME_FONT_SIZE,
    )
    _draw_left_text(
        c,
        fields["grade"],
        Page2Layout.GRADE[0],
        Page2Layout.GRADE[1],
        Page2Layout.GRADE_FONT_SIZE,
    )
    _draw_centered_text(
        c,
        fields["certificateNumber"],
        Page2Layout.FOOTER_CERT_NUMBER[0],
        Page2Layout.FOOTER_CERT_NUMBER[1],
        Page2Layout.FOOTER_FONT_SIZE,
    )
    _draw_centered_text(
        c,
        fields["issueDate"],
        Page2Layout.FOOTER_ISSUE_DATE[0],
        Page2Layout.FOOTER_ISSUE_DATE[1],
        Page2Layout.FOOTER_FONT_SIZE,
    )


def _build_overlay_page(
    fields: dict[str, str],
    page_index: int,
    page_size: tuple[float, float],
    *,
    plumbing: bool = False,
) -> Any:
    """Create a single-page transparent overlay PDF for merging onto a template page."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=page_size)
    if plumbing:
        if page_index != 0:
            raise ValueError(f"Unsupported plumbing template page index: {page_index}")
        _draw_plumbing_overlay(c, fields)
    elif page_index == 0:
        _draw_page1_overlay(c, fields)
    elif page_index == 1:
        _draw_page2_overlay(c, fields)
    else:
        raise ValueError(f"Unsupported template page index: {page_index}")
    c.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def generate_certificate_pdf(fields: dict[str, str], output_path: Path) -> Path:
    """
    Create a certificate PDF by merging text overlays onto the matching course template.
    Returns the resolved template path.
    """
    template_path = resolve_template_pdf(
        fields["courseName"],
        template_file=fields.get("templateFile"),
        templates_dir=TEMPLATES_DIR,
    )
    logger.info(
        "Using template=%s for courseName=%r certificateId=%s",
        template_path.name,
        fields["courseName"],
        fields["certificateId"],
    )
    template_reader = PdfReader(str(template_path))
    plumbing = _is_plumbing_template(template_path)
    required_pages = 1 if plumbing else 2
    if len(template_reader.pages) < required_pages:
        raise ValueError(
            f"Template {template_path.name} must contain at least {required_pages} page(s), "
            f"found {len(template_reader.pages)}"
        )

    writer = PdfWriter()
    page_indexes = (0,) if plumbing else (0, 1)
    for page_index in page_indexes:
        page_size = _template_page_size(template_reader, page_index)
        overlay_page = _build_overlay_page(
            fields, page_index, page_size, plumbing=plumbing
        )
        template_page = template_reader.pages[page_index]
        template_page.merge_page(overlay_page)
        writer.add_page(template_page)

    with output_path.open("wb") as pdf_file:
        writer.write(pdf_file)

    return template_path


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_generate_certificate():
    if not request.is_json:
        return jsonify({"success": False, "error": "Content-Type must be application/json"}), 415

    data = request.get_json(silent=True)
    fields, error = _validate_payload(data or {})
    if error:
        return error

    assert fields is not None
    filename = f"{uuid.uuid4()}.pdf"
    output_path = OUTPUT_DIR / filename

    try:
        template_path = generate_certificate_pdf(fields, output_path)
        logger.info(
            "Generated certificate pdf=%s certificateId=%s",
            filename,
            fields["certificateId"],
        )
    except FileNotFoundError as exc:
        logger.exception("Template missing during generation")
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        logger.exception("Invalid template during generation")
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("PDF generation failed")
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        return jsonify({"success": False, "error": "Failed to generate certificate PDF"}), 500

    return jsonify(
        {
            "success": True,
            "pdfUrl": _public_pdf_url(filename),
            "filename": filename,
            "certificateId": fields["certificateId"],
            "templateFile": template_path.name,
            "courseName": fields["courseName"],
        }
    ), 201


def _handle_serve_generated(filename: str):
    if not SAFE_FILENAME.match(filename):
        abort(404)
    file_path = OUTPUT_DIR / filename
    if not file_path.is_file():
        abort(404)
    return send_from_directory(
        OUTPUT_DIR,
        filename,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


# WSGI entrypoint for gunicorn
app = create_app()


# ---------------------------------------------------------------------------
# Local development entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the certificate PDF API locally")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", 5000)),
        help="Port to listen on (default: PORT env or 5000)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()
    debug = args.debug or os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=args.port, debug=debug)
