from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

FORM_PATH = (
    Path(__file__).resolve().parent.parent / "cfr_buyers_guides_english.pdf"
)

# Calibrated fallback overlay positions in PDF points. These are intentionally
# easy to tune after visual inspection, but are not used while named fields work.
MAKE_XY = (79.0, 647.5)
MODEL_XY = (202.0, 647.5)
YEAR_XY = (293.0, 647.5)
VIN_XY = (385.0, 647.5)

FIELD_FONT = "/Helv 10.5 Tf 0 g"

VERSION_CONFIG = {
    "as_is": {
        "page_index": 0,
        "field_prefix": "topmostSubform[0].BG-AsIs[0]",
    },
    "implied_only": {
        "page_index": 1,
        "field_prefix": "topmostSubform[0].BG-Implied[0]",
    },
}

FIELD_SUFFIXES = {
    "make": "VehicleMake[0]",
    "model": "Model[0]",
    "year": "Year[0]",
    "vin": "VIN[0]",
}


def _has_value(field: dict) -> bool:
    return field.get("/V") not in (None, "", "/Off")


def _flattened_pdf_is_valid(pdf_bytes: bytes, expected_values: list[str]) -> bool:
    reader = PdfReader(BytesIO(pdf_bytes))
    if len(reader.pages) != 1 or reader.trailer["/Root"].get("/AcroForm"):
        return False

    widgets = [
        annotation.get_object()
        for annotation in reader.pages[0].get("/Annots", [])
        if annotation.get_object().get("/Subtype") == "/Widget"
    ]
    if widgets:
        return False

    page_text = reader.pages[0].extract_text() or ""
    return all(not value or value in page_text for value in expected_values)


def render_buyers_guide(
    vin: str,
    make: str,
    model: str,
    year: str,
    version: str,
) -> bytes:
    if version not in VERSION_CONFIG:
        raise ValueError("version must be 'as_is' or 'implied_only'")

    source = PdfReader(FORM_PATH)
    if len(source.pages) != 3:
        raise ValueError("Official Buyers Guide form must contain exactly 3 pages.")

    config = VERSION_CONFIG[version]
    writer = PdfWriter()
    writer.append(
        source,
        pages=[config["page_index"]],
        import_outline=False,
    )

    fields = writer.get_fields() or {}
    field_names = {
        key: f"{config['field_prefix']}.{suffix}"
        for key, suffix in FIELD_SUFFIXES.items()
    }
    missing_fields = set(field_names.values()) - set(fields)
    if missing_fields:
        raise ValueError(
            "Official Buyers Guide vehicle fields are missing: "
            + ", ".join(sorted(missing_fields))
        )

    if any(_has_value(field) for field in fields.values()):
        raise ValueError("Official Buyers Guide template fields must all be blank.")

    values = {
        field_names["make"]: str(make).strip(),
        field_names["model"]: str(model).strip(),
        field_names["year"]: str(year).strip(),
        field_names["vin"]: str(vin).strip(),
    }

    expected_widget_names = set(FIELD_SUFFIXES.values())
    found_widget_names: set[str] = set()
    for annotation_reference in writer.pages[0].get("/Annots", []):
        annotation = annotation_reference.get_object()
        if annotation.get("/Subtype") != "/Widget":
            continue
        widget_name = str(annotation.get("/T", ""))
        if widget_name in expected_widget_names:
            annotation[NameObject("/DA")] = TextStringObject(FIELD_FONT)
            found_widget_names.add(widget_name)

    if found_widget_names != expected_widget_names:
        missing_widgets = expected_widget_names - found_widget_names
        raise ValueError(
            "Official Buyers Guide vehicle widgets are missing: "
            + ", ".join(sorted(missing_widgets))
        )

    writer.update_page_form_field_values(
        writer.pages[0],
        values,
        auto_regenerate=False,
        flatten=True,
    )
    writer.remove_annotations(subtypes="/Widget")
    writer.root_object.pop(NameObject("/AcroForm"), None)

    output = BytesIO()
    writer.write(output)
    pdf_bytes = output.getvalue()
    expected_values = list(values.values())
    if not _flattened_pdf_is_valid(pdf_bytes, expected_values):
        raise ValueError("Generated Buyers Guide could not be flattened safely.")
    return pdf_bytes
