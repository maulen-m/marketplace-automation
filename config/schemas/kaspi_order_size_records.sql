PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS record_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_size_records (
    record_key TEXT PRIMARY KEY,
    store_code TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    order_id TEXT,
    order_ref_hash TEXT NOT NULL,
    order_card_url TEXT,
    kaspi_status TEXT,
    order_placed_at TEXT,
    order_placed_date_local TEXT,
    order_placed_hour_local INTEGER,
    order_wave_bucket TEXT,
    product_offer_name TEXT,
    product_offer_id TEXT,
    product_family TEXT,
    sku_key TEXT,
    sku_id TEXT,
    ordered_offer_size TEXT,
    quantity INTEGER,
    current_google_board_my_size TEXT,
    google_board_size_recommendation TEXT,
    actual_fit_size_by_table TEXT,
    height_cm INTEGER,
    weight_kg INTEGER,
    fit_preference TEXT,
    preference_weight_adjustment_kg INTEGER NOT NULL DEFAULT 0,
    effective_weight_kg INTEGER,
    explicit_customer_size TEXT,
    reply_state TEXT NOT NULL DEFAULT 'unknown',
    size_source TEXT,
    size_confidence TEXT,
    size_fields_blank_for_manual_call INTEGER NOT NULL DEFAULT 0 CHECK (size_fields_blank_for_manual_call IN (0, 1)),
    manual_call_required INTEGER NOT NULL DEFAULT 0 CHECK (manual_call_required IN (0, 1)),
    template_status TEXT NOT NULL DEFAULT 'unknown',
    template_hash TEXT,
    request_sent_at TEXT,
    reply_seen_at TEXT,
    delivery_address TEXT,
    delivery_city TEXT,
    delivery_region TEXT,
    marketing_region TEXT,
    internal_marketing_campaign_id TEXT,
    acquisition_source TEXT,
    board_row_ref TEXT,
    data_backed_by TEXT,
    source_snapshot_at TEXT,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    pii_storage_scope TEXT NOT NULL DEFAULT 'local_private_data_dir',
    raw_customer_text_stored INTEGER NOT NULL DEFAULT 0 CHECK (raw_customer_text_stored = 0)
);

CREATE INDEX IF NOT EXISTS idx_order_size_records_store_seen
    ON order_size_records(store_code, last_seen_at);

CREATE INDEX IF NOT EXISTS idx_order_size_records_order_ref_hash
    ON order_size_records(order_ref_hash);

CREATE INDEX IF NOT EXISTS idx_order_size_records_manual_call
    ON order_size_records(manual_call_required, store_code);

CREATE INDEX IF NOT EXISTS idx_order_size_records_order_wave
    ON order_size_records(order_placed_date_local, order_placed_hour_local, delivery_region);

CREATE INDEX IF NOT EXISTS idx_order_size_records_product_family
    ON order_size_records(product_family, ordered_offer_size, actual_fit_size_by_table);

CREATE TABLE IF NOT EXISTS order_size_record_events (
    event_id TEXT PRIMARY KEY,
    record_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_status TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (record_key) REFERENCES order_size_records(record_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_order_size_record_events_record
    ON order_size_record_events(record_key, observed_at);

CREATE TABLE IF NOT EXISTS order_size_record_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    run_mode TEXT NOT NULL,
    merchant_ids_checked TEXT NOT NULL DEFAULT '[]',
    candidates_seen INTEGER NOT NULL DEFAULT 0,
    skipped_board_size_present INTEGER NOT NULL DEFAULT 0,
    template_messages_sent INTEGER NOT NULL DEFAULT 0,
    already_sent_skipped INTEGER NOT NULL DEFAULT 0,
    replies_observed INTEGER NOT NULL DEFAULT 0,
    sizes_classified INTEGER NOT NULL DEFAULT 0,
    blank_manual_call_rows INTEGER NOT NULL DEFAULT 0,
    google_board_updates_prepared INTEGER NOT NULL DEFAULT 0,
    google_board_updates_applied INTEGER NOT NULL DEFAULT 0,
    blockers_json TEXT NOT NULL DEFAULT '[]',
    live_customer_messages_sent INTEGER NOT NULL DEFAULT 0,
    external_writes_performed INTEGER NOT NULL DEFAULT 0,
    gate TEXT NOT NULL DEFAULT 'YELLOW',
    created_at TEXT NOT NULL
);
