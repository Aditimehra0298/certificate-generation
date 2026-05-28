"""
Certificate PDF Generation API

Flask service that generates multi-page certificate PDFs from image templates,
overlays dynamic fields, and serves generated files. Designed for n8n workflows
and deployment on Render / Railway via gunicorn.
"""

from __future__ import annotations

import io
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory, abort
from reportlab.lib.pagesizes import A4, portrait
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

import qrcode

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
GENERATED_DIR = BASE_DIR / "generated"
FONTS_DIR = BASE_DIR / "fonts"

PAGE1_TEMPLATE = TEMPLATES_DIR / "page1.png"
PAGE2_TEMPLATE = TEMPLATES_DIR / "page2.png"
FONT_REGULAR = FONTS_DIR / "times.ttf"
FONT_BOLD = FONTS_DIR / "timesbd.ttf"
FONT_FALLBACK_REGULAR = FONTS_DIR / "arial.ttf"
FONT_FALLBACK_BOLD = FONTS_DIR / "arialbd.ttf"

# Portrait A4 in points (matches 2480×3508 px templates at ~300 DPI)
PAGE_SIZE = portrait(A4)
PAGE_WIDTH, PAGE_HEIGHT = PAGE_SIZE

# Source template pixel dimensions (for coordinate conversion)
TEMPLATE_WIDTH_PX = 2480
TEMPLATE_HEIGHT_PX = 3508

# Writable output directory:
# - local/Render/Railway: project ./generated
# - Vercel serverless: /tmp/generated (read-only app bundle at /var/task)
OUTPUT_DIR = Path("/tmp/generated") if os.environ.get("VERCEL") else GENERATED_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Public base URL for pdfUrl in responses (set via env on Render/Railway)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# ---------------------------------------------------------------------------
# Text positioning — calibrated to templates/page1.png & page2.png
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
    CANDIDATE_NAME = _px(CANDIDATE_NAME_X_PX, CANDIDATE_NAME_BASELINE_Y_PX)
    CANDIDATE_FONT_SIZE = 38

    # Bottom-left metadata values — shared X so all values align after labels
    META_VALUE_X = _px(870, 0)[0]
    META_FONT_SIZE = 11
    ISSUE_DATE_Y = _px(0, 2699)[1]
    CERT_NUMBER_Y = _px(0, 2769)[1] - 4
    DELEGATE_NUMBER_Y = _px(0, 2886)[1]

    # Verification QR (top-left placeholder on template)
    QR_SIZE = 68
    QR_TOP_LEFT = _px(72, 72)


class Page2Layout:
    """Program Transcript — field positions (training program is on the template)."""

    # “Name :” row (y≈760) — total shift: right 86pt, down 32.5pt
    NAME = (_px(362, 760)[0] + 86, _px(362, 760)[1] - 32.5)
    NAME_FONT_SIZE = 11

    # “Overall Grade:” row (y≈850) — separate line from name
    GRADE = (_px(2276, 850)[0], _px(2276, 850)[1] - 3.4)
    GRADE_FONT_SIZE = 11

    # Centered in the blank band above “Certificate Number” / “Issue Date” labels
    FOOTER_CERT_NUMBER = (_px(310, 3210)[0] + 92, _px(310, 3210)[1] + 45)
    FOOTER_ISSUE_DATE = (_px(1240, 3210)[0] + 30, _px(1240, 3210)[1] + 48)
    FOOTER_FONT_SIZE = 11


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

    _register_fonts()
    _validate_startup_assets()

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"}), 200

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


def _register_fonts() -> None:
    """Register serif fonts to match certificate template typography."""
    global _fonts_registered
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


# ---------------------------------------------------------------------------
# Validation & helpers
# ---------------------------------------------------------------------------

def _validate_startup_assets() -> None:
    """Log warnings if templates or fonts are missing (fail on generate)."""
    for path in (PAGE1_TEMPLATE, PAGE2_TEMPLATE):
        if not path.is_file():
            logger.warning("Missing template: %s", path)
    if not FONT_REGULAR.is_file():
        logger.warning("Missing font: %s", FONT_REGULAR)


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

    cleaned = {
        "certificateId": str(data["certificateId"]).strip(),
        "candidateName": str(data["candidateName"]).strip(),
        "courseName": str(data["courseName"]).strip(),
        "grade": str(data["grade"]).strip(),
        "certificateNumber": str(data["certificateNumber"]).strip(),
        "delegateNumber": str(data["delegateNumber"]).strip(),
        "verifyUrl": verify_url,
        "issueDate": str(issue_date).strip() if issue_date else _default_issue_date(),
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
) -> None:
    """Draw horizontally centered text at (x, y)."""
    font = _font_name(bold=bold)
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
) -> None:
    """Draw left-aligned text with baseline at (x, y)."""
    font = _font_name(bold=bold)
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


def _draw_full_page_background(c: canvas.Canvas, image_path: Path) -> None:
    """Scale background image to cover entire portrait A4 page."""
    c.drawImage(
        ImageReader(str(image_path)),
        0,
        0,
        width=PAGE_WIDTH,
        height=PAGE_HEIGHT,
        preserveAspectRatio=False,
        mask="auto",
    )


def _make_qr_image_reader(url: str, box_size: int = 8) -> ImageReader:
    """Build a ReportLab ImageReader from a QR code for the given URL."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return ImageReader(buffer)


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def generate_certificate_pdf(fields: dict[str, str], output_path: Path) -> None:
    """
    Create a 2-page portrait A4 PDF with template backgrounds and overlays.
    """
    if not PAGE1_TEMPLATE.is_file() or not PAGE2_TEMPLATE.is_file():
        raise FileNotFoundError(
            f"Template images missing. Expected {PAGE1_TEMPLATE} and {PAGE2_TEMPLATE}"
        )

    c = canvas.Canvas(str(output_path), pagesize=PAGE_SIZE)
    c.setTitle(f"Certificate - {fields['certificateId']}")

    # --- Page 1: Certificate of Attainment ---
    _draw_full_page_background(c, PAGE1_TEMPLATE)

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
    c.showPage()

    # --- Page 2: Program Transcript ---
    _draw_full_page_background(c, PAGE2_TEMPLATE)

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
    c.showPage()

    c.save()


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
        generate_certificate_pdf(fields, output_path)
        logger.info(
            "Generated certificate pdf=%s certificateId=%s",
            filename,
            fields["certificateId"],
        )
    except FileNotFoundError as exc:
        logger.exception("Template missing during generation")
        return jsonify({"success": False, "error": str(exc)}), 500
    except Exception as exc:
        logger.exception("PDF generation failed")
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        return jsonify({"success": False, "error": "Failed to generate certificate PDF"}), 500

    return jsonify(
        {
            "success": True,
            "pdfUrl": _public_pdf_url(filename),
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
