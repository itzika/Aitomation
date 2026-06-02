-- Example schema for DB-discovery (DDL mode). Exercises PK, FK, UNIQUE, NOT NULL.

CREATE TABLE users (
    id         INTEGER PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    full_name  VARCHAR(120),
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE orders (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    total      NUMERIC(10, 2) NOT NULL DEFAULT 0,
    status     VARCHAR(20) NOT NULL,
    CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE order_items (
    order_id   INTEGER NOT NULL,
    sku        TEXT NOT NULL,
    quantity   INTEGER NOT NULL,
    PRIMARY KEY (order_id, sku),
    FOREIGN KEY (order_id) REFERENCES orders (id)
);
