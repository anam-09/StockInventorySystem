"""A small console-based stock and inventory program."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


DATA_FILE = Path(__file__).with_name("products.json")
DATABASE_FILE = Path(__file__).with_name("inventory.db")
MIGRATION_NAME = "products_json_v1"
PRODUCT_FIELDS = {
    "name", "quantity", "price", "sku", "category", "brand", "description",
    "unit", "purchase_price", "reorder_level", "preferred_supplier_id",
}


@contextmanager
def database_connection():
    """Open a SQLite connection and always close it after the operation."""
    connection = sqlite3.connect(DATABASE_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    """Create the product table and import the JSON source once, safely."""
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity >= 0),
                price REAL NOT NULL CHECK (price >= 0),
                extra_json TEXT NOT NULL DEFAULT '{}',
                migration_key TEXT UNIQUE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                contact_person TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                address TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Add product details without rebuilding the table or changing existing rows.
        product_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(products)")
        }
        product_additions = (
            ("sku", "TEXT"),
            ("category", "TEXT NOT NULL DEFAULT ''"),
            ("brand", "TEXT NOT NULL DEFAULT ''"),
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("unit", "TEXT NOT NULL DEFAULT 'each'"),
            ("purchase_price", "REAL CHECK (purchase_price IS NULL OR purchase_price >= 0)"),
            ("reorder_level", "INTEGER NOT NULL DEFAULT 5 CHECK (reorder_level >= 0)"),
            ("preferred_supplier_id", "INTEGER REFERENCES suppliers(id) ON DELETE SET NULL"),
        )
        for column_name, column_definition in product_additions:
            if column_name not in product_columns:
                connection.execute(
                    f"ALTER TABLE products ADD COLUMN {column_name} {column_definition}"
                )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku_unique "
            "ON products(sku) WHERE sku IS NOT NULL AND trim(sku) <> ''"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER NOT NULL,
                invoice_reference TEXT NOT NULL,
                purchase_date TEXT NOT NULL,
                total REAL NOT NULL CHECK (total >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE RESTRICT,
                UNIQUE (supplier_id, invoice_reference COLLATE NOCASE)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS purchase_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price REAL NOT NULL CHECK (unit_price >= 0),
                line_total REAL NOT NULL CHECK (line_total >= 0),
                stock_after INTEGER NOT NULL CHECK (stock_after >= 0),
                FOREIGN KEY (purchase_id) REFERENCES purchases(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                address TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        customer_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(customers)")
        }
        if "business_name" not in customer_columns:
            connection.execute(
                "ALTER TABLE customers ADD COLUMN business_name TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                invoice_reference TEXT NOT NULL COLLATE NOCASE UNIQUE,
                sale_date TEXT NOT NULL,
                total REAL NOT NULL CHECK (total >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sale_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price REAL NOT NULL CHECK (unit_price >= 0),
                line_total REAL NOT NULL CHECK (line_total >= 0),
                stock_after INTEGER NOT NULL CHECK (stock_after >= 0),
                FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT
            )
            """
        )
        purchase_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(purchases)")
        }
        if "due_date" not in purchase_columns:
            connection.execute("ALTER TABLE purchases ADD COLUMN due_date TEXT")
        sale_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(sales)")
        }
        if "due_date" not in sale_columns:
            connection.execute("ALTER TABLE sales ADD COLUMN due_date TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id INTEGER NOT NULL,
                payment_date TEXT NOT NULL,
                amount REAL NOT NULL CHECK (amount > 0),
                payment_method TEXT NOT NULL CHECK (length(trim(payment_method)) > 0),
                reference_number TEXT,
                notes TEXT,
                submission_token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (purchase_id) REFERENCES purchases(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                payment_date TEXT NOT NULL,
                amount REAL NOT NULL CHECK (amount > 0),
                payment_method TEXT NOT NULL CHECK (length(trim(payment_method)) > 0),
                reference_number TEXT,
                notes TEXT,
                submission_token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_purchases_supplier_date "
            "ON purchases(supplier_id, purchase_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_purchase_items_purchase "
            "ON purchase_items(purchase_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sales_customer_date "
            "ON sales(customer_id, sale_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sale_items_sale "
            "ON sale_items(sale_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sale_items_product "
            "ON sale_items(product_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_payments_purchase_date "
            "ON supplier_payments(purchase_id, payment_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_customer_payments_sale_date "
            "ON customer_payments(sale_id, payment_date)"
        )

        already_migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (MIGRATION_NAME,),
        ).fetchone()

        if already_migrated or not DATA_FILE.exists():
            return

        with DATA_FILE.open("r", encoding="utf-8") as file:
            source_products = json.load(file)
        if not isinstance(source_products, list):
            raise ValueError("products.json must contain a list of products.")

        for index, source_product in enumerate(source_products):
            if not isinstance(source_product, dict):
                raise ValueError(f"Product record {index + 1} is not an object.")
            try:
                name = str(source_product["name"])
                quantity = int(source_product["quantity"])
                price = float(source_product["price"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Product record {index + 1} has invalid fields.") from error
            if not name.strip() or quantity < 0 or price < 0:
                raise ValueError(f"Product record {index + 1} has invalid values.")

            extra_fields = {
                key: value
                for key, value in source_product.items()
                if key not in PRODUCT_FIELDS
            }
            connection.execute(
                """
                INSERT OR IGNORE INTO products
                    (name, quantity, price, extra_json, migration_key)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, quantity, price, json.dumps(extra_fields), f"products.json:{index}"),
            )

        connection.execute(
            "INSERT INTO schema_migrations (name) VALUES (?)",
            (MIGRATION_NAME,),
        )


def load_products():
    """Read product records from SQLite, including any preserved extra fields."""
    initialize_database()
    with database_connection() as connection:
        rows = connection.execute(
            "SELECT id, name, quantity, price, extra_json, sku, category, brand, "
            "description, unit, purchase_price, reorder_level, preferred_supplier_id "
            "FROM products ORDER BY id"
        ).fetchall()

    products = []
    for row in rows:
        product = json.loads(row["extra_json"])
        product.update(
            {
                "id": row["id"],
                "name": row["name"],
                "quantity": row["quantity"],
                "price": row["price"],
                "sku": row["sku"] or "",
                "category": row["category"],
                "brand": row["brand"],
                "description": row["description"],
                "unit": row["unit"],
                "purchase_price": row["purchase_price"],
                "reorder_level": row["reorder_level"],
                "preferred_supplier_id": row["preferred_supplier_id"],
            }
        )
        products.append(product)
    return products


def save_products(products):
    """Save a product list to SQLite while keeping existing row IDs stable."""
    try:
        initialize_database()
        with database_connection() as connection:
            current_rows = connection.execute("SELECT id FROM products").fetchall()
            current_ids = {row["id"] for row in current_rows}
            retained_ids = set()

            for product in products:
                product_id = product.get("id")
                extra_fields = {
                    key: value
                    for key, value in product.items()
                    if key not in PRODUCT_FIELDS and key != "id"
                }
                product_values = (
                    product["name"],
                    int(product["quantity"]),
                    float(product["price"]),
                    json.dumps(extra_fields),
                    product.get("sku") or None,
                    product.get("category", ""),
                    product.get("brand", ""),
                    product.get("description", ""),
                    product.get("unit") or "each",
                    product.get("purchase_price"),
                    int(product.get("reorder_level", 5)),
                    product.get("preferred_supplier_id") or None,
                )

                if product_id in current_ids:
                    connection.execute(
                        """
                        UPDATE products
                        SET name = ?, quantity = ?, price = ?, extra_json = ?, sku = ?,
                            category = ?, brand = ?, description = ?, unit = ?,
                            purchase_price = ?, reorder_level = ?, preferred_supplier_id = ?
                        WHERE id = ?
                        """,
                        (*product_values, product_id),
                    )
                    retained_ids.add(product_id)
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO products
                            (name, quantity, price, extra_json, sku, category, brand,
                             description, unit, purchase_price, reorder_level, preferred_supplier_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        product_values,
                    )
                    product["id"] = cursor.lastrowid
                    retained_ids.add(cursor.lastrowid)

            for removed_id in current_ids - retained_ids:
                connection.execute("DELETE FROM products WHERE id = ?", (removed_id,))
        return True
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
        print(f"Could not save products: {error}")
        return False


def add_product(products):
    """Ask for product details, validate them, and save the new product."""
    name = input("Product name: ").strip()
    if not name:
        print("Product name cannot be empty.")
        return

    try:
        quantity = int(input("Quantity: "))
        if quantity < 0:
            print("Quantity cannot be negative.")
            return

        price = float(input("Price: "))
        if price < 0:
            print("Price cannot be negative.")
            return
    except ValueError:
        print("Quantity must be a whole number and price must be a number.")
        return

    product = {"name": name, "quantity": quantity, "price": price}
    products.append(product)

    if save_products(products):
        print(f"Added {name} to inventory.")
    else:
        # Keep the in-memory list consistent if saving failed.
        products.pop()


def view_products():
    """Load saved products and display each product and its total value."""
    products = load_products()

    if not products:
        print("No products are saved yet.")
        return

    print("\nSaved Products")
    for product in products:
        total_value = product["quantity"] * product["price"]
        print(f"Name: {product['name']}")
        print(f"Quantity: {product['quantity']}")
        print(f"Price: {product['price']:.2f}")
        print(f"Total value: {total_value:.2f}")
        print("-" * 30)


def search_product(products):
    """Find a product by name and display its stock details."""
    search_name = input("Enter the product name to search for: ").strip()

    if not search_name:
        print("Please enter a product name.")
        return

    # Convert both names to lowercase so the search ignores letter case.
    for product in products:
        if product["name"].lower() == search_name.lower():
            total_value = product["quantity"] * product["price"]
            print("\nProduct found")
            print(f"Name: {product['name']}")
            print(f"Quantity: {product['quantity']}")
            print(f"Price: {product['price']:.2f}")
            print(f"Total stock value: {total_value:.2f}")
            return

    print(f"No product named '{search_name}' was found.")


def update_stock(products):
    """Find a product, ask for its new quantity, and save the change."""
    search_name = input("Enter the product name to update: ").strip()

    if not search_name:
        print("Please enter a product name.")
        return

    for product in products:
        if product["name"].lower() == search_name.lower():
            print(f"Current quantity for {product['name']}: {product['quantity']}")

            try:
                new_quantity = int(input("Enter the new quantity: "))
                if new_quantity < 0:
                    print("Quantity cannot be negative.")
                    return
            except ValueError:
                print("Please enter a whole number for the quantity.")
                return

            old_quantity = product["quantity"]
            product["quantity"] = new_quantity

            if save_products(products):
                print(f"Quantity for {product['name']} updated to {new_quantity}.")
            else:
                # Restore the old value if the updated list could not be saved.
                product["quantity"] = old_quantity
            return

    print(f"No product named '{search_name}' was found.")


def delete_product(products):
    """Ask for confirmation before removing a product and saving the list."""
    search_name = input("Enter the product name to delete: ").strip()

    if not search_name:
        print("Please enter a product name.")
        return

    for index, product in enumerate(products):
        if product["name"].lower() == search_name.lower():
            print(f"Product found: {product['name']}")
            confirmation = input("Delete this product? Enter Y to confirm or N to cancel: ").strip().lower()

            if confirmation == "y":
                deleted_product = products.pop(index)
                if save_products(products):
                    print(f"{deleted_product['name']} was deleted.")
                else:
                    # Put the product back if saving the deletion failed.
                    products.insert(index, deleted_product)
            elif confirmation == "n":
                print("Deletion cancelled.")
            else:
                print("Please enter Y or N. Deletion cancelled.")
            return

    print(f"No product named '{search_name}' was found.")


def low_stock_alert():
    """Show products whose quantity is at or below a chosen threshold."""
    products = load_products()

    if not products:
        print("No products are available to check.")
        return

    try:
        threshold = int(input("Enter the minimum stock quantity threshold: "))
        if threshold < 0:
            print("The threshold cannot be negative.")
            return
    except ValueError:
        print("Please enter a whole number for the threshold.")
        return

    matching_products = []
    for product in products:
        if product["quantity"] <= threshold:
            matching_products.append(product)

    if not matching_products:
        print(f"No products have stock at or below {threshold}.")
        return

    print(f"\nProducts with stock at or below {threshold}:")
    for product in matching_products:
        print(f"Name: {product['name']} | Quantity: {product['quantity']}")


def inventory_summary():
    """Calculate and display a summary of the saved inventory."""
    products = load_products()

    if not products:
        print("No products are saved yet, so there is no inventory summary to show.")
        return

    # Start each total at zero, then add each product's figures in the loop.
    total_products = len(products)
    total_units = 0
    total_value = 0
    zero_quantity_products = 0

    for product in products:
        total_units += product["quantity"]
        total_value += product["quantity"] * product["price"]

        if product["quantity"] == 0:
            zero_quantity_products += 1

    print("\nInventory Summary")
    print(f"Different products: {total_products}")
    print(f"Total units in stock: {total_units}")
    print(f"Total inventory value: {total_value:.2f}")
    print(f"Products with zero quantity: {zero_quantity_products}")


def stock_in(products):
    """Add received units to a product's current stock and save the result."""
    search_name = input("Enter the product name receiving stock: ").strip()
    if not search_name:
        print("Please enter a product name.")
        return

    for product in products:
        if product["name"].lower() == search_name.lower():
            try:
                amount = int(input("Enter the quantity received: "))
                if amount <= 0:
                    print("The quantity must be a positive whole number.")
                    return
            except ValueError:
                print("Please enter a positive whole number.")
                return

            previous_quantity = product["quantity"]
            product["quantity"] += amount

            if save_products(products):
                print(f"Previous quantity: {previous_quantity}")
                print(f"Updated quantity: {product['quantity']}")
            else:
                # Restore the previous quantity if saving did not work.
                product["quantity"] = previous_quantity
            return

    print(f"No product named '{search_name}' was found.")


def stock_out(products):
    """Remove units from a product, if enough stock is available, and save."""
    search_name = input("Enter the product name removing stock: ").strip()
    if not search_name:
        print("Please enter a product name.")
        return

    for product in products:
        if product["name"].lower() == search_name.lower():
            try:
                amount = int(input("Enter the quantity removed: "))
                if amount <= 0:
                    print("The quantity must be a positive whole number.")
                    return
            except ValueError:
                print("Please enter a positive whole number.")
                return

            previous_quantity = product["quantity"]
            if amount > previous_quantity:
                print(
                    f"Insufficient stock. Only {previous_quantity} "
                    f"unit(s) of {product['name']} are available."
                )
                return

            product["quantity"] -= amount

            if save_products(products):
                print(f"Previous quantity: {previous_quantity}")
                print(f"Updated quantity: {product['quantity']}")
            else:
                # Restore the previous quantity if saving did not work.
                product["quantity"] = previous_quantity
            return

    print(f"No product named '{search_name}' was found.")


def show_menu():
    """Display the options currently available in the program."""
    print("\nStock and Inventory Management")
    print("1. Add product")
    print("2. View products")
    print("3. Search product")
    print("4. Update stock")
    print("5. Delete product")
    print("6. Low-stock alert")
    print("7. Inventory summary report")
    print("8. Stock In")
    print("9. Stock Out")
    print("0. Exit")


def main():
    """Load saved data and keep showing the menu until the user exits."""
    products = load_products()

    while True:
        show_menu()
        choice = input("Choose an option: ").strip()

        if choice == "1":
            add_product(products)
        elif choice == "2":
            view_products()
        elif choice == "3":
            search_product(products)
        elif choice == "4":
            update_stock(products)
        elif choice == "5":
            delete_product(products)
        elif choice == "6":
            low_stock_alert()
        elif choice == "7":
            inventory_summary()
        elif choice == "8":
            stock_in(products)
        elif choice == "9":
            stock_out(products)
        elif choice == "0":
            print("Goodbye!")
            break
        else:
            print("Please enter a choice from 0 to 9.")


if __name__ == "__main__":
    main()
