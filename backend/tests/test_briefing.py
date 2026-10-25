from __future__ import annotations

from app.briefing.validate import is_factual, sanitize, validate_sections

DIGEST = {
    "allowed_citations": {"[RM-DOC-0101, 2025-03-02]": "2025-03-02", "[TT-A01 r09, 2025-03]": "2025-03"},
    "ruling_ids": {
        "ESTABLISHED": ["RUL:R1-T-C01:R1"],
        "CONTESTED": [],
        "CONCEDED": ["RUL:R1-L-C02:R1"],
        "REJECTED": [],
    },
    "established": [
        {
            "ruling": "RUL:R1-T-C01:R1",
            "statement": "The canal closed on 2025-03-02",
            "citations": [{"cite": "[RM-DOC-0101, 2025-03-02]"}],
        }
    ],
    "contested": [],
    "conceded": [
        {
            "ruling": "RUL:R1-L-C02:R1",
            "statement": "Imports fell 95 percent in March",
            "citations": [{"cite": "[TT-A01 r09, 2025-03]"}],
        }
    ],
}


def _sections(**over: object) -> dict:  # type: ignore[type-arg]
    s = {
        "executive_summary": "The canal closed to all traffic on 2025-03-02 [RM-DOC-0101, 2025-03-02].",
        "established": "RUL:R1-T-C01:R1: the canal closed [RM-DOC-0101, 2025-03-02].",
        "contested": "None.",
        "conceded": "RUL:R1-L-C02:R1: imports fell 95 percent [TT-A01 r09, 2025-03].",
        "outlook": "Trade stays 10 percent lower next quarter [TT-A01 r09, 2025-03].",
        "assumptions": [{"id": "A1", "text": "closure holds", "ruling_ids": ["RUL:R1-T-C01:R1"]}],
    }
    s.update(over)
    return s


def test_valid_briefing_passes() -> None:
    chk = validate_sections(_sections(), DIGEST)
    assert chk.ok, chk.errors


def test_citation_outside_digest_or_wrong_date_fails() -> None:
    chk = validate_sections(_sections(outlook="Exposure is 20 percent [RM-DOC-0999, 2025-03-01]."), DIGEST)
    assert not chk.ok and "[RM-DOC-0999, 2025-03-01]" in chk.invalid_citations
    chk = validate_sections(_sections(outlook="Exposure is 20 percent [RM-DOC-0101, 2025-03-09]."), DIGEST)
    assert not chk.ok


def test_uncited_factual_sentence_missing_ruling_and_bad_assumption() -> None:
    chk = validate_sections(
        _sections(
            executive_summary="Brenmark lost 40 percent of its trade in March.",
            established="Nothing to add.",
            assumptions=[{"id": "A1", "text": "x", "ruling_ids": []}],
        ),
        DIGEST,
    )
    assert chk.uncited_sentences and chk.missing_rulings.get("established") == ["RUL:R1-T-C01:R1"]
    assert any("assumption A1" in e for e in chk.errors)
    fixed = sanitize(
        _sections(
            executive_summary="Brenmark lost 40 percent of its trade in March.", established="Nothing to add."
        ),
        DIGEST,
        chk,
    )
    assert "40 percent" not in fixed["executive_summary"] and "RUL:R1-T-C01:R1" in fixed["established"]
    assert validate_sections(fixed, DIGEST).ok


def test_meta_sentences_are_exempt() -> None:
    assert not is_factual("This case is INCOMPLETE: BOTH_SIDES_FAILED.")
    assert is_factual("Brenmark imports fell by 40 percent in March.")
