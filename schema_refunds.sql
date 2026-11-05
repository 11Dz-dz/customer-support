-- Additive migration: existing applications remain readable. No payment state is inferred.
CREATE TABLE IF NOT EXISTS refund_application_details (
 refund_id text PRIMARY KEY REFERENCES refund_requests(refund_id) ON DELETE CASCADE,
 submitted_by uuid NOT NULL,
 scope text NOT NULL CHECK(scope='whole_order'),
 order_total_snapshot numeric(19,4) NOT NULL CHECK(order_total_snapshot>=0)
);
CREATE TABLE IF NOT EXISTS refund_submission_keys (
 user_id uuid NOT NULL, client_request_id uuid NOT NULL,
 order_id integer NOT NULL, reason text NOT NULL,
 refund_id text NOT NULL REFERENCES refund_requests(refund_id) ON DELETE CASCADE,
 PRIMARY KEY(user_id,client_request_id)
);
CREATE TABLE IF NOT EXISTS refund_reviews (
 review_id bigserial PRIMARY KEY,
 refund_id text NOT NULL UNIQUE REFERENCES refund_requests(refund_id) ON DELETE CASCADE,
 reviewer_id uuid NOT NULL,
 decision text NOT NULL CHECK(decision IN ('approved','rejected')),
 note text NOT NULL CHECK(length(btrim(note)) BETWEEN 1 AND 2000),
 reviewed_at timestamptz NOT NULL DEFAULT now()
);
