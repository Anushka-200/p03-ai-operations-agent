"""One conversation at a time per customer.

Analogy: one patient folder, one doctor at a time. Two texts from the same person arriving together
would otherwise both load the same saved conversation, each add their message and save, and the
second save could silently drop the first message. A Postgres advisory lock makes the second wait.

The lock is "transaction level": Postgres releases it by itself at COMMIT or ROLLBACK, so a crashed
worker can never leave a customer locked. Different customers use different keys and never wait.
"""

from typing import Any


def lock_conversation(conn: Any, customer_id: int) -> None:
    """Block until no other open transaction holds this customer's conversation lock."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"conv:{customer_id}",))
