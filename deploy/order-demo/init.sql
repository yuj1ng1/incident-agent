CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    item TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0)
);

INSERT INTO orders (id, item, quantity)
VALUES (1, 'demo-order', 1)
ON CONFLICT (id) DO NOTHING;
