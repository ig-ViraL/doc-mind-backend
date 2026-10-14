import os
import random
import uuid
from datetime import date, datetime, timedelta

import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/mcp_test")

N = 100

COUNTRIES = ["US", "IN", "CN", "GH", "DE", "AE", "KR", "BR", "RU", "FR", "GB", "JP", "AU", "CA", "MX", "NG", "ZA", "IT", "ES", "PL"]
STATES = ["CA", "TX", "NY", "FL", "WA", "IL", "MA", "OH", "PA", "GA"]
ORDER_STATUS = ["pending", "paid", "shipped", "delivered", "cancelled"]
PAYMENT_METHODS = ["card", "upi", "bank_transfer", "cod", "wallet"]
REVIEW_COMMENTS = [
    "Great product!",
    "Highly recommended.",
    "Good value for money.",
    "Exceeded expectations.",
    "Not what I expected.",
    "Average quality.",
    "Would buy again.",
    "Fast delivery, happy customer.",
    "Decent but pricey.",
    "Excellent build quality.",
    "Arrived damaged.",
    "Perfect, love it!",
]

FIRST_NAMES = ["Alice", "Bob", "Clara", "David", "Eva", "Fatima", "George", "Hannah", "Ivan", "Julia", "Karl", "Laura", "Mike", "Nina", "Oscar", "Priya", "Quinn", "Rachel", "Sam", "Tina"]
LAST_NAMES = ["Martin", "Singh", "Zhou", "Osei", "Muller", "AlSayed", "Kim", "Silva", "Petrov", "Santos", "Brown", "Chen", "Davis", "Evans", "Fischer", "Garcia", "Harris", "Iyer", "Jones", "Khan"]

PRODUCT_ADJ = ["Pro", "Ultra", "Lite", "Max", "Mini", "Plus", "Smart", "Eco", "Turbo", "Flex"]
PRODUCT_NOUN = ["Headphones", "Keyboard", "Shoes", "Lamp", "Bottle", "Desk", "Stand", "Speaker", "Camera", "Charger", "Blender", "Chair", "Monitor", "Mouse", "Backpack", "Jacket", "Watch", "Router", "Microphone", "Tripod"]
CATEGORIES = ["Electronics", "Fitness", "Books", "Home", "Clothing", "Beauty", "Toys", "Sports", "Garden", "Food"]


DDL = """
CREATE TABLE IF NOT EXISTS customers (
  customer_id  SERIAL PRIMARY KEY,
  full_name    TEXT NOT NULL,
  email        TEXT UNIQUE NOT NULL,
  country      TEXT NOT NULL,
  joined_at    DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_addresses (
  address_id    SERIAL PRIMARY KEY,
  customer_id   INT NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
  line1         TEXT NOT NULL,
  city          TEXT NOT NULL,
  state         TEXT NOT NULL,
  postal_code   TEXT NOT NULL,
  country       TEXT NOT NULL,
  is_default    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS suppliers (
  supplier_id  SERIAL PRIMARY KEY,
  name         TEXT NOT NULL,
  email        TEXT UNIQUE NOT NULL,
  country      TEXT NOT NULL,
  created_at   TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
  product_id   SERIAL PRIMARY KEY,
  supplier_id  INT NOT NULL REFERENCES suppliers(supplier_id),
  sku          TEXT UNIQUE NOT NULL,
  name         TEXT NOT NULL,
  category     TEXT NOT NULL,
  unit_price   NUMERIC(10,2) NOT NULL,
  active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS warehouses (
  warehouse_id  SERIAL PRIMARY KEY,
  code          TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  country       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
  inventory_id  SERIAL PRIMARY KEY,
  warehouse_id  INT NOT NULL REFERENCES warehouses(warehouse_id) ON DELETE CASCADE,
  product_id    INT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
  on_hand       INT NOT NULL CHECK (on_hand >= 0),
  reserved      INT NOT NULL CHECK (reserved >= 0),
  updated_at    TIMESTAMP NOT NULL,
  UNIQUE (warehouse_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
  order_id     SERIAL PRIMARY KEY,
  customer_id  INT NOT NULL REFERENCES customers(customer_id),
  address_id   INT NOT NULL REFERENCES customer_addresses(address_id),
  order_date   DATE NOT NULL,
  status       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
  item_id     SERIAL PRIMARY KEY,
  order_id    INT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
  product_id  INT NOT NULL REFERENCES products(product_id),
  quantity    INT NOT NULL CHECK (quantity > 0),
  unit_price  NUMERIC(10,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  payment_id      SERIAL PRIMARY KEY,
  order_id        INT NOT NULL UNIQUE REFERENCES orders(order_id) ON DELETE CASCADE,
  paid_at         TIMESTAMP NOT NULL,
  amount          NUMERIC(10,2) NOT NULL CHECK (amount >= 0),
  method          TEXT NOT NULL,
  transaction_ref TEXT UNIQUE NOT NULL,
  status          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
  review_id    SERIAL PRIMARY KEY,
  customer_id  INT NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
  product_id   INT NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
  rating       SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
  review_date  DATE NOT NULL,
  comment      TEXT
);
"""


def rand_date(start_year=2023, end_year=2024):
    start = date(start_year, 1, 1)
    end = date(end_year, 12, 31)
    return start + timedelta(days=random.randint(0, (end - start).days))


def rand_ts(start_year=2023, end_year=2024):
    d = rand_date(start_year, end_year)
    return datetime(d.year, d.month, d.day, random.randint(0, 23), random.randint(0, 59), random.randint(0, 59))


def gen_customers(n: int):
    rows = []
    seen = set()
    for i in range(n):
        fn = random.choice(FIRST_NAMES)
        ln = random.choice(LAST_NAMES)
        email = f"{fn.lower()}.{ln.lower()}{i}@example.com"
        while email in seen:
            email = f"{fn.lower()}.{ln.lower()}{i}_{random.randint(0, 999)}@example.com"
        seen.add(email)
        rows.append((f"{fn} {ln}", email, random.choice(COUNTRIES), rand_date(2022, 2024)))
    return rows


def gen_addresses(n: int):
    rows = []
    for customer_id in range(1, n + 1):
        line1 = f"{random.randint(10, 9999)} {random.choice(['Main', 'Oak', 'Pine', 'Maple', 'Cedar', 'Hill', 'Lake'])} St"
        city = random.choice(["Springfield", "Riverside", "Franklin", "Greenville", "Bristol", "Clinton", "Fairview", "Salem", "Madison", "Georgetown"])
        state = random.choice(STATES)
        postal = f"{random.randint(10000, 99999)}"
        country = random.choice(COUNTRIES)
        rows.append((customer_id, line1, city, state, postal, country, True))
    return rows


def gen_suppliers(n: int):
    rows = []
    for i in range(n):
        name = f"{random.choice(['Acme', 'Globex', 'Initech', 'Umbrella', 'Soylent', 'Stark', 'Wayne', 'Wonka'])} Supply {i + 1}"
        email = f"supplier{i + 1}@example.com"
        rows.append((name, email, random.choice(COUNTRIES), rand_ts(2021, 2024)))
    return rows


def gen_products(n: int, num_suppliers: int):
    rows = []
    for i in range(n):
        supplier_id = random.randint(1, num_suppliers)
        sku = f"SKU-{i + 1:04d}-{random.choice(['A', 'B', 'C', 'D'])}"
        name = f"{random.choice(PRODUCT_ADJ)} {random.choice(PRODUCT_NOUN)} {i + 1}"
        category = random.choice(CATEGORIES)
        price = round(random.uniform(9.99, 499.99), 2)
        active = random.random() > 0.05
        rows.append((supplier_id, sku, name, category, price, active))
    return rows


def gen_warehouses(n: int):
    rows = []
    for i in range(n):
        code = f"WH-{i + 1:03d}"
        name = f"Warehouse {i + 1}"
        rows.append((code, name, random.choice(COUNTRIES)))
    return rows


def gen_inventory(n: int, num_warehouses: int, num_products: int):
    combos = set()
    rows = []
    while len(rows) < n:
        wh = random.randint(1, num_warehouses)
        pr = random.randint(1, num_products)
        if (wh, pr) in combos:
            continue
        combos.add((wh, pr))
        on_hand = random.randint(0, 500)
        reserved = random.randint(0, min(50, on_hand))
        rows.append((wh, pr, on_hand, reserved, rand_ts(2023, 2024)))
    return rows


def gen_orders(n: int, num_customers: int, num_addresses: int):
    rows = []
    for _ in range(n):
        customer_id = random.randint(1, num_customers)
        address_id = random.randint(1, num_addresses)
        status = random.choice(ORDER_STATUS)
        rows.append((customer_id, address_id, rand_date(2023, 2024), status))
    return rows


def gen_order_items(n: int, num_orders: int, num_products: int):
    rows = []
    for _ in range(n):
        order_id = random.randint(1, num_orders)
        product_id = random.randint(1, num_products)
        qty = random.randint(1, 5)
        unit_price = round(random.uniform(9.99, 499.99), 2)
        rows.append((order_id, product_id, qty, unit_price))
    return rows


def gen_payments(n: int, num_orders: int):
    used_orders = set()
    rows = []
    while len(rows) < n:
        order_id = random.randint(1, num_orders)
        if order_id in used_orders:
            continue
        used_orders.add(order_id)
        amount = round(random.uniform(10, 2000), 2)
        method = random.choice(PAYMENT_METHODS)
        tx_ref = f"TX-{uuid.uuid4()}"
        status = random.choice(["initiated", "succeeded", "failed", "refunded"])
        rows.append((order_id, rand_ts(2023, 2024), amount, method, tx_ref, status))
    return rows


def gen_reviews(n: int, num_customers: int, num_products: int):
    rows = []
    for _ in range(n):
        rows.append(
            (
                random.randint(1, num_customers),
                random.randint(1, num_products),
                random.randint(1, 5),
                rand_date(2023, 2024),
                random.choice(REVIEW_COMMENTS),
            )
        )
    return rows


def main():
    random.seed(42)

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()

    print("Creating tables...")
    cur.execute(DDL)

    print("Truncating tables (restart identity)...")
    cur.execute(
        """
        TRUNCATE TABLE
          reviews,
          payments,
          order_items,
          orders,
          inventory,
          warehouses,
          products,
          suppliers,
          customer_addresses,
          customers
        RESTART IDENTITY CASCADE;
        """
    )

    print(f"Seeding {N} customers...")
    cur.executemany(
        "INSERT INTO customers (full_name, email, country, joined_at) VALUES (%s,%s,%s,%s)",
        gen_customers(N),
    )

    print(f"Seeding {N} customer_addresses...")
    cur.executemany(
        "INSERT INTO customer_addresses (customer_id, line1, city, state, postal_code, country, is_default) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        gen_addresses(N),
    )

    print(f"Seeding {N} suppliers...")
    cur.executemany(
        "INSERT INTO suppliers (name, email, country, created_at) VALUES (%s,%s,%s,%s)",
        gen_suppliers(N),
    )

    print(f"Seeding {N} products...")
    cur.executemany(
        "INSERT INTO products (supplier_id, sku, name, category, unit_price, active) VALUES (%s,%s,%s,%s,%s,%s)",
        gen_products(N, N),
    )

    print(f"Seeding {N} warehouses...")
    cur.executemany(
        "INSERT INTO warehouses (code, name, country) VALUES (%s,%s,%s)",
        gen_warehouses(N),
    )

    print(f"Seeding {N} inventory rows...")
    cur.executemany(
        "INSERT INTO inventory (warehouse_id, product_id, on_hand, reserved, updated_at) VALUES (%s,%s,%s,%s,%s)",
        gen_inventory(N, N, N),
    )

    print(f"Seeding {N} orders...")
    cur.executemany(
        "INSERT INTO orders (customer_id, address_id, order_date, status) VALUES (%s,%s,%s,%s)",
        gen_orders(N, N, N),
    )

    print(f"Seeding {N} order_items...")
    cur.executemany(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (%s,%s,%s,%s)",
        gen_order_items(N, N, N),
    )

    print(f"Seeding {N} payments...")
    cur.executemany(
        "INSERT INTO payments (order_id, paid_at, amount, method, transaction_ref, status) VALUES (%s,%s,%s,%s,%s,%s)",
        gen_payments(N, N),
    )

    print(f"Seeding {N} reviews...")
    cur.executemany(
        "INSERT INTO reviews (customer_id, product_id, rating, review_date, comment) VALUES (%s,%s,%s,%s,%s)",
        gen_reviews(N, N, N),
    )

    cur.execute(
        """
        SELECT 'customers', COUNT(*) FROM customers UNION ALL
        SELECT 'customer_addresses', COUNT(*) FROM customer_addresses UNION ALL
        SELECT 'suppliers', COUNT(*) FROM suppliers UNION ALL
        SELECT 'products', COUNT(*) FROM products UNION ALL
        SELECT 'warehouses', COUNT(*) FROM warehouses UNION ALL
        SELECT 'inventory', COUNT(*) FROM inventory UNION ALL
        SELECT 'orders', COUNT(*) FROM orders UNION ALL
        SELECT 'order_items', COUNT(*) FROM order_items UNION ALL
        SELECT 'payments', COUNT(*) FROM payments UNION ALL
        SELECT 'reviews', COUNT(*) FROM reviews
        ORDER BY 1;
        """
    )
    print("\nFinal row counts:")
    for table, count in cur.fetchall():
        print(f"  {table:<20} {count}")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()

