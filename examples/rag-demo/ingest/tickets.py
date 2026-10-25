"""Embed support tickets into the vector index, one document per ticket."""

from langchain_core.documents import Document

SELECT_TICKETS = "SELECT id, tenant_id, subject, body FROM public.support_tickets WHERE updated_at > %s"


def ticket_text(ticket) -> str:
    return f"{ticket['subject']}\n\n{ticket['body']}\nFrom: {ticket['customer_email']}"


def index_tickets(conn, vector_store, since):
    rows = conn.execute(SELECT_TICKETS, (since,)).fetchall()
    docs = [
        Document(
            page_content=ticket_text(row),
            metadata={"id": str(row["id"]), "tenant_id": str(row["tenant_id"])},
        )
        for row in rows
    ]
    vector_store.add_documents(docs)
