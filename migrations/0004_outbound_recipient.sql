-- Messages to someone other than the customer (page the on-call technician, alert the owner).
-- NULL recipient = the customer on the row. Kept as plain text: a technician phone is personal data
-- and the audit log / logs only ever show a hash of it.
ALTER TABLE outbound_messages ADD COLUMN recipient text;
