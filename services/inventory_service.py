"""Driver-kit stock helpers for the DOBS HR Final step. Writes to the
shared inventory_items / driver_inventory_issuances / driver_inventory_handovers
tables that DMS owns (created by DMS migrations 2026_09_07_210000 and
2026_09_07_230000). Mirrors the rules in DMS's App\\Services\\DriverInventoryService:
  - issue:  stock_quantity -= 1  (allowed to go negative -- a count
            mismatch must never block onboarding; DMS flags negatives)
  - a Driver Card never returns to stock (restock_on_return = False)
  - the items issued in one HR Final action form ONE handover, printable as a
    single signed acknowledgement (terms & conditions) at
    https://dms.speedlogi.sa/dms/driver-inventory/handover/<pdf_token>
The caller is responsible for db.session.commit().
"""
import secrets

from extensions import db
from models import InventoryItem, DriverInventoryIssuance, DriverInventoryHandover


def _find_or_make_item(item_type, business_id):
    q = InventoryItem.query.filter_by(item_type=item_type).filter(InventoryItem.deleted_at.is_(None))
    q = q.filter(InventoryItem.business_id == business_id) if business_id else q.filter(InventoryItem.business_id.is_(None))
    item = q.first()
    if item is None:
        # No stock row configured yet -- create a zero-stock one so the
        # issuance still records (stock goes negative, surfaced in DMS).
        item = InventoryItem(
            item_type=item_type,
            business_id=business_id,
            stock_quantity=0,
            restock_on_return=(item_type != InventoryItem.TYPE_DRIVER_CARD),
            is_active=True,
        )
        db.session.add(item)
        db.session.flush()
    return item


def issue_kit_at_hr_final(driver, assigned_platforms, form, issued_by_id, issued_by_name=None):
    """Read issue_shirt_<bid> / issue_bag_<bid> / issue_driver_card from the
    submitted form. Shirt + Bag must be ticked for EVERY assigned platform.
    Returns (error, handover_token):
      - (error_string, None)  -> nothing written, blocking message for HR
      - (None, token)         -> issued OK, token for the signed handover PDF
      - (None, None)          -> everything was already issued, nothing to do
    issued_by_id / issued_by_name identify the HR user doing the issuing.
    """
    missing = []
    to_issue = []  # (item_type, business_id)

    for platform in assigned_platforms or []:
        bid = platform["id"]
        pname = platform.get("name") or f"#{bid}"
        if form.get(f"issue_shirt_{bid}"):
            to_issue.append((InventoryItem.TYPE_SHIRT, bid))
        else:
            missing.append(f"Shirt — {pname}")
        if form.get(f"issue_bag_{bid}"):
            to_issue.append((InventoryItem.TYPE_BAG, bid))
        else:
            missing.append(f"Bag — {pname}")

    if not assigned_platforms:
        return ("This driver has no assigned platform yet — assign a platform ID before completing HR Final.", None)

    if missing:
        return ("Issue the platform kit first — still to tick: " + ", ".join(missing) + ".", None)

    if form.get("issue_driver_card"):
        to_issue.append((InventoryItem.TYPE_DRIVER_CARD, None))

    handover = None
    for item_type, business_id in to_issue:
        item = _find_or_make_item(item_type, business_id)
        already = (
            DriverInventoryIssuance.query
            .filter_by(driver_id=driver.id, inventory_item_id=item.id)
            .filter(DriverInventoryIssuance.deleted_at.is_(None))
            .first()
        )
        if already:
            continue

        if handover is None:
            handover = DriverInventoryHandover(
                driver_id=driver.id,
                issued_by=issued_by_id,
                issued_by_name=issued_by_name,
                source=DriverInventoryHandover.SOURCE_DOBS_HR_FINAL,
                pdf_token=secrets.token_urlsafe(36),
            )
            db.session.add(handover)
            db.session.flush()  # get handover.id
            handover.handover_number = f"KIT-{handover.id:06d}"

        db.session.add(DriverInventoryIssuance(
            handover_id=handover.id,
            driver_id=driver.id,
            inventory_item_id=item.id,
            quantity=1,
            issued_by=issued_by_id,
            issued_by_name=issued_by_name,
            source=DriverInventoryIssuance.SOURCE_DOBS_HR_FINAL,
        ))
        item.stock_quantity = (item.stock_quantity or 0) - 1

    return (None, handover.pdf_token if handover else None)
