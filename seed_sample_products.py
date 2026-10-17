"""Insert fictional demonstration products without changing existing stock."""

from main import database_connection, initialize_database


SAMPLE_PRODUCTS = (
    ("DEMO-AC-15T", "1.5 Ton Split AC", "Air Conditioners", "FrostLine Demo",
     "Fictional demonstration model FL-15T", "each", 28000, 34000, 5, 2),
    ("DEMO-AC-20T", "2 Ton Split AC", "Air Conditioners", "FrostLine Demo",
     "Fictional demonstration model FL-20T", "each", 38000, 46000, 3, 1),
    ("DEMO-PIPE-CU", "Copper Pipe", "Installation Materials", "CopperCraft Demo",
     "Fictional demonstration copper refrigerant pipe", "metre", 350, 500, 50, 10),
    ("DEMO-INSTALL-KIT", "AC Installation Kit", "Installation Materials", "InstallMate Demo",
     "Fictional demonstration split AC installation kit", "each", 800, 1200, 15, 3),
    ("DEMO-REMOTE", "AC Remote", "AC Accessories", "RemoteWorks Demo",
     "Fictional demonstration universal AC remote", "each", 250, 450, 10, 2),
    ("DEMO-CAP-AC", "Capacitor", "AC Parts", "CoolParts Demo",
     "Fictional demonstration AC replacement capacitor", "each", 120, 250, 20, 5),
    ("DEMO-DRAIN-PIPE", "Drain Pipe", "Installation Materials", "FlowFlex Demo",
     "Fictional demonstration condensate drain pipe", "metre", 40, 80, 40, 10),
    ("DEMO-STABILIZER", "AC Stabilizer", "Electrical Protection", "VoltGuard Demo",
     "Fictional demonstration voltage stabilizer", "each", 1500, 2200, 8, 2),
)


def seed_sample_products():
    """Insert each SKU only if neither it nor its sample name already exists."""
    initialize_database()
    added = 0
    skipped = 0
    with database_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for sku, name, category, brand, description, unit, cost, price, quantity, reorder in SAMPLE_PRODUCTS:
            existing = connection.execute(
                "SELECT id FROM products WHERE sku = ? OR name = ? COLLATE NOCASE LIMIT 1",
                (sku, name),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            connection.execute(
                """
                INSERT INTO products
                    (name, quantity, price, sku, category, brand, description, unit,
                     purchase_price, reorder_level, preferred_supplier_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (name, quantity, price, sku, category, brand, description, unit,
                 cost, reorder),
            )
            added += 1
    print(f"Sample seed complete: added {added}, skipped existing {skipped}.")
    print("Starting quantities are initial demo balances; no purchase or stock movement was recorded.")


if __name__ == "__main__":
    seed_sample_products()
