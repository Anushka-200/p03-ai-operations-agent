-- Phase 1 additions, kept in their own file: 0001/0002 have already run on existing databases,
-- so a change is a NEW numbered file, never an edit of an old one.

-- A discount is applied ON TOP of the price quoted at booking time (bookings.price_usd stays as quoted).
ALTER TABLE bookings
    ADD COLUMN discount_pct  numeric(4,1) NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
    ADD COLUMN refunded_usd  numeric(8,2) NOT NULL DEFAULT 0 CHECK (refunded_usd >= 0);

-- Bulk follow-ups are first drafted (nothing is sent), then sent only after the owner approves.
ALTER TABLE approvals DROP CONSTRAINT approvals_status_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_status_check
    CHECK (status IN ('DRAFT', 'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED'));
