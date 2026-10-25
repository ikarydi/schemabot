CREATE TABLE public.support_tickets (
    id           UUID PRIMARY KEY,
    tenant_id    UUID        NOT NULL,
    subject      TEXT        NOT NULL,
    body         TEXT        NOT NULL,
    customer_email TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX support_tickets_tenant_idx ON public.support_tickets (tenant_id);
