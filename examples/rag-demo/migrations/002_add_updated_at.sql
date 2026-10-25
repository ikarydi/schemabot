ALTER TABLE public.support_tickets ADD COLUMN updated_at TIMESTAMPTZ;
UPDATE public.support_tickets SET updated_at = created_at;
ALTER TABLE public.support_tickets ALTER COLUMN updated_at SET NOT NULL;
