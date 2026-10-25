from __future__ import annotations

import pytest

from app.ingest.header import doc_meta_from_fields, parse_header
from app.ingest.parsers import ExtractError
from app.ingest.parsers.docx import extract_docx
from app.ingest.parsers.pdf import extract_pdf
from app.ingest.parsers.txt import extract_txt
from app.ingest.sniff import sniff
from app.ingest.trade_table import parse_trade_table


def test_header_aliases_and_multi_values() -> None:
    text = (
        "ID: RM-X-1\nHeadline: Something\nSource: Runewire\nPublished: 2025-03-02\nType: News Report\n"
        "Event: EVT-9\nCountries: A; B\nSectors: Grain, Textiles\nReliability: Tier 2\n\nBody text here."
    )
    h = parse_header(text)
    assert h.found and h.body == "Body text here."
    m = doc_meta_from_fields(h.fields)
    assert m.doc_id == "RM-X-1" and m.outlet == "Runewire" and m.tier == 2
    assert m.pub_date is not None and m.pub_date.isoformat() == "2025-03-02"
    assert m.countries == ["A", "B"] and m.sectors == ["grain", "textiles"]
    assert m.source_type == "news_report" and m.event_id == "EVT-9"


def test_header_background_and_validation_flags() -> None:
    h = parse_header("Doc ID: X\nPublication Date: 03/02/2025\nEvent ID: Background\nTier: 7\n\nbody")
    m = doc_meta_from_fields(h.fields)
    assert m.is_background and m.event_id == "background"
    assert "BAD_DATE" in m.flags and "BAD_TIER" in m.flags and m.tier is None


def test_no_header_keeps_whole_text_as_body() -> None:
    h = parse_header("Just a paragraph of news with no metadata.\nAnother line.")
    assert not h.found and h.body.startswith("Just a paragraph")


def test_sniff_by_magic_bytes() -> None:
    assert sniff(b"") == "empty"
    assert sniff(b"%PDF-1.7 ...") == "pdf"
    assert sniff(b"PK\x03\x04....word/document.xml") == "docx"
    assert sniff(b"plain text") == "txt"
    assert sniff(bytes(range(256)) * 4) == "unsupported"


def test_txt_repairs_latin1() -> None:
    ex = extract_txt("Caf\xe9 owners near the port".encode("latin-1"))
    assert ex.repaired and "owners near the port" in ex.text


def test_corrupt_pdf_raises_structured_error() -> None:
    with pytest.raises(ExtractError) as e:
        extract_pdf(b"%PDF-1.4 garbage that is not a pdf")
    assert e.value.code == "CORRUPT_PDF"


def test_corrupt_docx_raises_structured_error() -> None:
    with pytest.raises(ExtractError) as e:
        extract_docx(b"PK\x03\x04 not really a zip")
    assert e.value.code == "CORRUPT_DOCX"


TABLE = """Table ID: TT-9
Country Pair: Aland -> Bland
Sector: grain
Unit: kt
Period: 2025-01 to 2025-06
Event ID: EVT-9

row | month | value | volume
01 | 2025-01 | 100 | 10
02 | 2025-02 | 1,200.5 | 11
02 | 2025-02 | 1,200.5 | 11
03 | 2025-03 | NA | 12
04 | 2025-04 | 9x9 | 13
04 | 2025-05 | 101 | 14
05 | 2025-05 | -3 | 15
06 | 2025-06 | 99
"""


def test_trade_table_row_quality_flags() -> None:
    t = parse_trade_table(TABLE)
    assert (t.table_id, t.exporter, t.importer, t.sector) == ("TT-9", "Aland", "Bland", "grain")
    assert (t.period_start, t.period_end) == ("2025-01", "2025-06")
    q = [(r.row, r.quality) for r in t.rows]
    # the first VALID occurrence of a row number is kept: the second "04" is OK because the first was damaged
    assert q == [
        (1, "OK"),
        (2, "OK"),
        (2, "DUPLICATE_ROW"),
        (3, "MISSING_VALUE"),
        (4, "BAD_NUMBER"),
        (4, "OK"),
        (5, "BAD_NUMBER"),
        (6, "TRUNCATED"),
    ]
    assert t.rows[1].value == 1200.5
    assert any(f.startswith("TRUNCATED_GAP") or f.startswith("MISSING_MONTHS") for f in t.flags)


def test_trade_table_whitespace_delimited() -> None:
    text = "Table ID: TT-W\nCountry Pair: A to B\nSector: grain\nPeriod: 2025-01 to 2025-02\n\nrow  month  value  volume\n01  2025-01  5  1\n02  2025-02  6  2\n"
    t = parse_trade_table(text)
    assert [r.quality for r in t.rows] == ["OK", "OK"] and t.rows[1].value == 6
