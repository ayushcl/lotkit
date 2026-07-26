import re
from decimal import Decimal, InvalidOperation
from html import escape
from io import BytesIO
from typing import Any

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

NAVY = HexColor("#1F2A44")
NAVY_LIGHT = HexColor("#E9EDF5")
INK = HexColor("#172033")
GREY = HexColor("#667085")
LIGHT_GREY = HexColor("#D8DEE9")
ROW_GREY = HexColor("#F7F8FA")
WHITE = HexColor("#FFFFFF")

LITRE_PATTERN = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*[lL]\b")
CYLINDER_PATTERN = re.compile(
    r"\b(?P<number>\d+(?:\.0+)?)\s*(?:cylinders?|cyl\.?)\b",
    re.IGNORECASE,
)
NORMALIZED_CYLINDER_PATTERN = re.compile(
    r"\b\d+(?:\.0+)?-cyl\b",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clean_value(value: Any) -> str:
    return _text(value) or "—"


def _first_value(data: dict, *keys: str) -> str:
    for key in keys:
        value = _text(data.get(key))
        if value:
            return value
    return ""


def _format_litre(value: str) -> str:
    match = re.search(r"\d+(?:\.\d+)?", value)
    if not match:
        return ""
    try:
        return f"{float(match.group()):.1f}L"
    except ValueError:
        return ""


def _format_cylinders(value: str) -> str:
    match = re.search(r"\d+(?:\.0+)?", value)
    if not match:
        return ""
    number = float(match.group())
    display = str(int(number)) if number.is_integer() else str(number)
    return f"{display}-cyl"


def _normalize_engine_text(value: str) -> str:
    def replace_litre(match: re.Match) -> str:
        return f"{float(match.group('number')):.1f}L"

    def replace_cylinders(match: re.Match) -> str:
        return _format_cylinders(match.group("number"))

    value = LITRE_PATTERN.sub(replace_litre, value)
    value = CYLINDER_PATTERN.sub(replace_cylinders, value)
    value = re.sub(r"\s*[,;|]\s*", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def format_engine(vehicle: dict) -> str:
    engine = _normalize_engine_text(_text(vehicle.get("engine")))
    displacement = _first_value(
        vehicle,
        "displacement",
        "displacement_l",
        "DisplacementL",
    )
    cylinders = _first_value(
        vehicle,
        "cylinders",
        "engine_cylinders",
        "EngineCylinders",
    )
    fuel = _first_value(
        vehicle,
        "fuel",
        "fuel_type",
        "fuel_type_primary",
        "FuelTypePrimary",
    )

    parts = [engine] if engine else []
    if displacement and not LITRE_PATTERN.search(engine):
        formatted_displacement = _format_litre(displacement)
        if formatted_displacement:
            parts.insert(0, formatted_displacement)
    if cylinders and not (
        CYLINDER_PATTERN.search(engine) or NORMALIZED_CYLINDER_PATTERN.search(engine)
    ):
        formatted_cylinders = _format_cylinders(cylinders)
        if formatted_cylinders:
            parts.append(formatted_cylinders)
    if fuel and fuel.casefold() not in engine.casefold():
        parts.append(fuel)

    return " ".join(parts)


def _format_price(price: str | None) -> str:
    value = _text(price)
    if not value:
        return "Call for price"

    numeric = value.replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(numeric)
    except InvalidOperation:
        return value

    if amount == amount.to_integral_value():
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def _format_mileage(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    numeric = text.replace(",", "").strip()
    if numeric.isdigit():
        return f"{int(numeric):,} miles"
    return text


def _paragraph(text: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(_text(text)), style)


def build_sticker_pdf(
    vin: str,
    vehicle: dict,
    dealer: dict,
    price: str | None,
    extras: dict | None,
) -> bytes:
    dealer = dealer or {}
    extras = extras or {}
    output = BytesIO()

    dealer_name = _text(dealer.get("name")) or "WINDOW STICKER"
    dealer_address = _text(dealer.get("address"))
    dealer_phone = _text(dealer.get("phone"))
    footer_text = (
        _text(dealer.get("footer_text"))
        or "Please see a sales associate for full details."
    )

    logo_reader = None
    logo_size: tuple[float, float] | None = None
    logo_bytes = dealer.get("logo_bytes")
    if isinstance(logo_bytes, (bytes, bytearray)) and logo_bytes:
        try:
            logo_reader = ImageReader(BytesIO(bytes(logo_bytes)))
            logo_size = logo_reader.getSize()
        except Exception:
            logo_reader = None
            logo_size = None

    document = SimpleDocTemplate(
        output,
        pagesize=letter,
        leftMargin=40,
        rightMargin=40,
        topMargin=124,
        bottomMargin=68,
        title=f"{vin} Window Sticker",
        author=dealer_name,
    )

    header_name_style = ParagraphStyle(
        "HeaderName",
        fontName="Helvetica-Bold",
        fontSize=21,
        leading=23,
        textColor=WHITE,
        spaceAfter=0,
    )
    header_contact_style = ParagraphStyle(
        "HeaderContact",
        fontName="Helvetica",
        fontSize=8.5,
        leading=10,
        textColor=WHITE,
        spaceAfter=0,
    )
    footer_style = ParagraphStyle(
        "Footer",
        fontName="Helvetica-Oblique",
        fontSize=7.5,
        leading=9,
        textColor=GREY,
    )

    def draw_page(canvas, doc) -> None:
        page_width, page_height = letter
        band_height = 104
        band_bottom = page_height - band_height

        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, band_bottom, page_width, band_height, fill=1, stroke=0)

        rendered_logo_width = 0.0
        if logo_reader is not None and logo_size is not None:
            source_width, source_height = logo_size
            scale = min(118 / source_width, 58 / source_height)
            logo_width = source_width * scale
            logo_height = source_height * scale
            logo_x = page_width - 40 - logo_width
            logo_y = band_bottom + (band_height - logo_height) / 2
            try:
                canvas.drawImage(
                    logo_reader,
                    logo_x,
                    logo_y,
                    width=logo_width,
                    height=logo_height,
                    preserveAspectRatio=True,
                    mask="auto",
                )
                rendered_logo_width = logo_width + 24
            except Exception:
                rendered_logo_width = 0.0

        name_width = page_width - 80 - rendered_logo_width
        name = _paragraph(dealer_name, header_name_style)
        _, name_height = name.wrap(name_width, 48)
        name.drawOn(canvas, 40, page_height - 27 - name_height)

        contact = "  |  ".join(
            part for part in (dealer_address, dealer_phone) if part
        )
        if contact:
            contact_paragraph = _paragraph(contact, header_contact_style)
            _, contact_height = contact_paragraph.wrap(name_width, 24)
            contact_paragraph.drawOn(
                canvas,
                40,
                band_bottom + 13,
            )

        canvas.setStrokeColor(LIGHT_GREY)
        canvas.setLineWidth(0.6)
        canvas.line(40, 49, page_width - 40, 49)
        footer = _paragraph(footer_text, footer_style)
        _, footer_height = footer.wrap(page_width - 80, 30)
        footer.drawOn(canvas, 40, 38 - footer_height)
        canvas.restoreState()

    title_style = ParagraphStyle(
        "VehicleTitle",
        fontName="Helvetica-Bold",
        fontSize=29,
        leading=33,
        textColor=INK,
        spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "VehicleMeta",
        fontName="Courier",
        fontSize=8.5,
        leading=11,
        textColor=GREY,
    )
    section_style = ParagraphStyle(
        "Section",
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=NAVY,
        spaceBefore=4,
        spaceAfter=7,
    )
    label_style = ParagraphStyle(
        "SpecLabel",
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=GREY,
    )
    value_style = ParagraphStyle(
        "SpecValue",
        fontName="Helvetica",
        fontSize=10,
        leading=12,
        textColor=INK,
    )
    feature_style = ParagraphStyle(
        "Feature",
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=INK,
        leftIndent=2,
    )
    price_label_style = ParagraphStyle(
        "PriceLabel",
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        alignment=TA_CENTER,
        textColor=GREY,
    )
    price_style = ParagraphStyle(
        "Price",
        fontName="Helvetica-Bold",
        fontSize=27,
        leading=30,
        alignment=TA_CENTER,
        textColor=NAVY,
    )

    year = _first_value(vehicle, "year", "ModelYear")
    make = _first_value(vehicle, "make", "Make")
    model = _first_value(vehicle, "model", "Model")
    trim = _first_value(vehicle, "trim", "Trim")
    main_title = " ".join(part for part in (year, make, model) if part)
    if not main_title:
        main_title = "Vehicle Details"
    title_markup = f"<b>{escape(main_title)}</b>"
    if trim:
        title_markup += (
            f' <font name="Helvetica" size="21" color="#667085">'
            f"{escape(trim)}</font>"
        )

    meta_parts = [f"VIN: {vin}"]
    stock_number = _text(extras.get("stock_number"))
    if stock_number:
        meta_parts.append(f"Stock #: {stock_number}")

    story = [
        KeepTogether(
            [
                Paragraph(title_markup, title_style),
                Paragraph(
                    escape("    |    ".join(meta_parts)),
                    meta_style,
                ),
            ]
        ),
        Spacer(1, 17),
        Paragraph("Vehicle Specifications", section_style),
    ]

    specifications = [
        ("Engine", format_engine(vehicle)),
        (
            "Transmission",
            _first_value(vehicle, "transmission", "TransmissionStyle"),
        ),
        ("Drivetrain", _first_value(vehicle, "drive", "drivetrain", "DriveType")),
        ("Body", _first_value(vehicle, "body", "BodyClass")),
        ("Doors", _first_value(vehicle, "doors", "Doors")),
        ("Exterior Colour", _text(extras.get("exterior_colour"))),
        ("Interior Colour", _text(extras.get("interior_colour"))),
        ("Mileage", _format_mileage(extras.get("mileage"))),
    ]
    specification_rows = [
        [_paragraph(label.upper(), label_style), _paragraph(value, value_style)]
        for label, value in specifications
        if value
    ]
    if specification_rows:
        specification_table = Table(
            specification_rows,
            colWidths=[1.55 * inch, document.width - 1.55 * inch],
            hAlign=TA_LEFT,
        )
        table_style = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LINEBELOW", (0, 0), (-1, -1), 0.45, LIGHT_GREY),
        ]
        for row_number in range(0, len(specification_rows), 2):
            table_style.append(
                ("BACKGROUND", (0, row_number), (-1, row_number), ROW_GREY)
            )
        specification_table.setStyle(TableStyle(table_style))
        story.append(specification_table)
    else:
        story.append(_paragraph("Details available on request.", value_style))

    raw_features = extras.get("features")
    features = (
        [_text(feature) for feature in raw_features if _text(feature)]
        if isinstance(raw_features, list)
        else []
    )
    if features:
        story.extend(
            [
                Spacer(1, 15),
                Paragraph("Features &amp; Equipment", section_style),
            ]
        )
        feature_rows = []
        for index in range(0, len(features), 2):
            row = []
            for feature in features[index : index + 2]:
                row.append(
                    Paragraph(
                        f"&#8226;&nbsp;&nbsp;{escape(feature)}",
                        feature_style,
                    )
                )
            if len(row) == 1:
                row.append("")
            feature_rows.append(row)
        feature_table = Table(
            feature_rows,
            colWidths=[document.width / 2, document.width / 2],
        )
        feature_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(feature_table)

    price_table = Table(
        [
            [Paragraph("PRICE", price_label_style)],
            [Paragraph(escape(_format_price(price)), price_style)],
        ],
        colWidths=[3.05 * inch],
        hAlign=TA_RIGHT,
    )
    price_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY_LIGHT),
                ("BOX", (0, 0), (-1, -1), 1.2, NAVY),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
                ("TOPPADDING", (0, 1), (-1, 1), 2),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
            ]
        )
    )
    story.extend([Spacer(1, 19), price_table])

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return output.getvalue()
