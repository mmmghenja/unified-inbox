-- Секрет коннектора на внутреннем сокете. Раздел «Аутентификация на внутреннем сокете» docs/architecture.md.
-- Только hub.db. В таблице лежит только sha256(секрет), не сам секрет (см. connector_auth.py). Секрет привязан
-- к account_id: один коннектор — один текущий секрет (ротация в v1 не нужна, поэтому PK, не отдельный id).

CREATE TABLE IF NOT EXISTS connector_secret (
    account_id INTEGER PRIMARY KEY REFERENCES account (id),
    hash TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
