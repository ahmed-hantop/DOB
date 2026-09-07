from models import DriverDocument


def test_driver_document_type_is_free_text_not_a_closed_enum():
    """document_type must accept ANY value DMS writes, not just DOBS's own
    vocabulary. A closed db.Enum() here crashed every dashboard load for any
    driver holding a document_type outside its list -- happened for real with
    qiwa_contract (fixed 2026-08-31 by widening the list) and again with
    company_contract/promissory_note (2026-09-03), because a fixed list can
    never keep up with a column two independent systems write to freely.
    """
    column_type = DriverDocument.__table__.c.document_type.type

    assert getattr(column_type, "enums", None) is None, (
        "document_type must not be a closed SQLAlchemy Enum -- see "
        "20260903_document_type_free_text.py for why"
    )
    assert column_type.python_type is str

    # DOBS's own known values, plus the DMS-only ones that broke production --
    # none of these should be rejected merely for being "unrecognized".
    known_and_dms_only_values = (
        "iqama", "passport", "visa", "license", "medical", "contract",
        "mobile", "qiwa_contract", "other",
        "company_contract", "promissory_note",
    )
    for value in known_and_dms_only_values:
        doc = DriverDocument(driver_id=1, document_type=value, file_path="x")
        assert doc.document_type == value
