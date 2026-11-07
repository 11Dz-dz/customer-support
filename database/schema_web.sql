CREATE TABLE IF NOT EXISTS web_users (
 user_id uuid PRIMARY KEY, username text UNIQUE NOT NULL,
 display_name text NOT NULL, password_hash text NOT NULL,
 role text NOT NULL CHECK(role IN ('user','admin')), customer_id integer,
 active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS web_sessions (
 token_hash text PRIMARY KEY, user_id uuid NOT NULL REFERENCES web_users(user_id),
 csrf_token text NOT NULL, expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS web_login_limits (
 key text PRIMARY KEY, failures integer NOT NULL, window_start timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS web_conversations (
 conversation_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES web_users(user_id),
 thread_id text UNIQUE NOT NULL, title text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS web_conversations_owner ON web_conversations(user_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS web_runs (
 run_id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES web_conversations(conversation_id),
 client_request_id uuid NOT NULL, message text NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','running','completed','failed','interrupted')),
 created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 UNIQUE(conversation_id,client_request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS web_one_active_run ON web_runs(conversation_id)
 WHERE status IN ('pending','running');
CREATE TABLE IF NOT EXISTS web_run_events (
 event_id bigserial PRIMARY KEY, run_id uuid NOT NULL REFERENCES web_runs(run_id),
 event_type text NOT NULL CHECK(event_type IN ('progress','text','done','error')),
 payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS web_run_events_cursor ON web_run_events(run_id,event_id);
ALTER TABLE web_runs ADD COLUMN IF NOT EXISTS input_closed boolean NOT NULL DEFAULT false;
ALTER TABLE web_runs ADD COLUMN IF NOT EXISTS cancel_requested boolean NOT NULL DEFAULT false;
ALTER TABLE web_run_events DROP CONSTRAINT IF EXISTS web_run_events_event_type_check;
ALTER TABLE web_run_events ADD CONSTRAINT web_run_events_event_type_check
 CHECK(event_type IN ('progress','text','done','error','input'));
CREATE TABLE IF NOT EXISTS web_run_inputs (
 sequence_id bigserial UNIQUE NOT NULL, input_id uuid PRIMARY KEY,
 run_id uuid NOT NULL REFERENCES web_runs(run_id),
 conversation_id uuid NOT NULL REFERENCES web_conversations(conversation_id),
 client_request_id uuid NOT NULL, message text NOT NULL,
 mode text NOT NULL CHECK(mode IN ('steer','followup')),
 phase text NOT NULL DEFAULT 'pending' CHECK(phase IN ('pending','included','answered','cancelled','interrupted')),
 batch_id uuid, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(conversation_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS web_inputs_pending ON web_run_inputs(run_id,sequence_id);

-- Public registration limits are shared across processes and service restarts.
CREATE TABLE IF NOT EXISTS web_registration_limits (
 key text PRIMARY KEY, attempts integer NOT NULL, window_start timestamptz NOT NULL
);
