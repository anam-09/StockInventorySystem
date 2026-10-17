import csv
import io
import math
import json
import re
import sqlite3
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, Response, flash, redirect, render_template, request, url_for
from main import PRODUCT_FIELDS, database_connection, initialize_database, load_products, save_products

app = Flask(__name__)
# Flash messages use Flask's session cookie; no persistent login session is used.
app.config["SECRET_KEY"] = secrets.token_hex(32)
TRANSACTIONS_FILE = Path(__file__).with_name("transactions.json")
LOW_STOCK_THRESHOLD = 5
PAYMENT_METHODS = ("Cash", "UPI", "Bank transfer", "Card", "Cheque", "Other")


def load_transactions():
    """Read transaction history, creating an empty JSON list when needed."""
    if not TRANSACTIONS_FILE.exists():
        if not save_transactions([]):
            raise OSError("Could not create transactions.json.")
        return []

    with TRANSACTIONS_FILE.open("r", encoding="utf-8") as file:
        transactions = json.load(file)

    if not isinstance(transactions, list):
        raise ValueError("Transaction history must be a JSON list.")
    return transactions


def save_transactions(transactions):
    """Safely write the transaction list without replacing it partially."""
    temporary_file = TRANSACTIONS_FILE.with_name("transactions.json.tmp")
    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(transactions, file, indent=4)
        temporary_file.replace(TRANSACTIONS_FILE)
        return True
    except OSError:
        if temporary_file.exists():
            try:
                temporary_file.unlink()
            except OSError:
                pass
        return False


def optional_iso_date(value, label):
    """Return an optional YYYY-MM-DD date or raise a helpful validation error."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(f"Enter a valid {label}.") from error
    return value


def payment_state(invoice, payment_rows):
    """Compute invoice balance and status from its payment records."""
    total = Decimal(str(invoice["total"]))
    paid = sum((Decimal(str(row["amount"])) for row in payment_rows), Decimal("0"))
    outstanding = max(Decimal("0"), total - paid)
    status = "Paid" if outstanding == 0 else ("Partially Paid" if paid > 0 else "Unpaid")
    due_date = invoice["due_date"]
    overdue = bool(outstanding > 0 and due_date and due_date < date.today().isoformat())
    return {
        "amount_paid": float(paid),
        "outstanding": float(outstanding),
        "payment_status": status,
        "is_overdue": overdue,
    }


def fetch_invoice_payments(connection, payment_table, invoice_column, invoice_id):
    """Fetch immutable payment rows for an invoice (table/column are internal constants)."""
    return connection.execute(
        f"SELECT * FROM {payment_table} WHERE {invoice_column} = ? ORDER BY payment_date DESC, id DESC",
        (invoice_id,),
    ).fetchall()


def enrich_invoice_payment(connection, invoice, payment_table, invoice_column):
    payment_rows = fetch_invoice_payments(
        connection, payment_table, invoice_column, invoice["id"]
    )
    enriched = dict(invoice)
    enriched.update(payment_state(invoice, payment_rows))
    return enriched, payment_rows


def record_invoice_payment(invoice_kind, invoice_id):
    """Record a partial or full payment/receipt without touching inventory stock."""
    initialize_database()
    if invoice_kind == "purchase":
        invoice_table, invoice_column, payment_table = "purchases", "purchase_id", "supplier_payments"
        detail_endpoint = "purchase_detail_page"
        invoice_label = "purchase invoice"
    else:
        invoice_table, invoice_column, payment_table = "sales", "sale_id", "customer_payments"
        detail_endpoint = "sale_detail_page"
        invoice_label = "sales invoice"

    destination = url_for(detail_endpoint, **({"purchase_id": invoice_id} if invoice_kind == "purchase" else {"sale_id": invoice_id}))
    try:
        payment_date = optional_iso_date(request.form.get("payment_date"), "payment date")
        if not payment_date:
            raise ValueError("Payment date is required.")
        try:
            amount = Decimal(request.form.get("amount", "").strip())
        except InvalidOperation as error:
            raise ValueError("Enter a valid payment amount.") from error
        if not amount.is_finite() or amount <= 0 or not math.isfinite(float(amount)):
            raise ValueError("Payment amount must be a positive valid amount.")
        try:
            rounded_amount = amount.quantize(Decimal("0.01"))
        except InvalidOperation as error:
            raise ValueError("Payment amount is too large to save.") from error
        if amount != rounded_amount:
            raise ValueError("Payment amount can have no more than two decimal places.")
        payment_method = request.form.get("payment_method", "").strip()
        if payment_method not in PAYMENT_METHODS:
            raise ValueError("Choose a valid payment method.")
        reference_number = request.form.get("reference_number", "").strip() or None
        notes = request.form.get("notes", "").strip() or None
        submission_token = request.form.get("submission_token", "").strip()
        if not submission_token or len(submission_token) > 128:
            raise ValueError("Payment form token is missing. Reload the invoice and try again.")

        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            invoice = connection.execute(
                f"SELECT id, total FROM {invoice_table} WHERE id = ?", (invoice_id,)
            ).fetchone()
            if invoice is None:
                raise ValueError(f"That {invoice_label} could not be found.")
            payment_rows = fetch_invoice_payments(connection, payment_table, invoice_column, invoice_id)
            total = Decimal(str(invoice["total"]))
            paid = sum((Decimal(str(row["amount"])) for row in payment_rows), Decimal("0"))
            remaining = max(Decimal("0"), total - paid)
            if amount > remaining:
                raise ValueError(
                    f"Payment exceeds the remaining balance of Rs {remaining:.2f}."
                )
            connection.execute(
                f"""INSERT INTO {payment_table}
                    ({invoice_column}, payment_date, amount, payment_method,
                     reference_number, notes, submission_token)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (invoice_id, payment_date, float(amount), payment_method,
                 reference_number, notes, submission_token),
            )
    except sqlite3.IntegrityError as error:
        if "unique" in str(error).lower():
            flash("This payment form was already submitted; no duplicate payment was added.", "warning")
        else:
            flash("Payment could not be saved. No payment was recorded.", "danger")
        return redirect(destination)
    except (ValueError, OSError, sqlite3.Error) as error:
        flash(str(error) or "Payment could not be saved.", "danger")
        return redirect(destination)

    flash(f"Payment of Rs {amount:.2f} was recorded.", "success")
    return redirect(destination)


def validate_product_form(form_data, products, ignore_index=None):
    """Check submitted product fields and return product data or an error."""
    name = form_data.get("name", "").strip()
    quantity_text = form_data.get("quantity", "").strip()
    price_text = form_data.get("price", "").strip()
    sku = form_data.get("sku", "").strip()
    purchase_price_text = form_data.get("purchase_price", "").strip()
    category = form_data.get("category", "").strip()
    brand = form_data.get("brand", "").strip()
    description = form_data.get("description", "").strip()
    unit = form_data.get("unit", "each").strip() or "each"
    preferred_supplier_text = form_data.get("preferred_supplier_id", "").strip()
    reorder_text = form_data.get("reorder_level", "5").strip()

    if not name:
        return None, "Product name cannot be empty."

    for index, product in enumerate(products):
        if index != ignore_index and product["name"].casefold() == name.casefold():
            return None, "A product with that name already exists."
        if sku and index != ignore_index and (product.get("sku") or "").casefold() == sku.casefold():
            return None, "A product with that SKU already exists."

    try:
        quantity = int(quantity_text)
        if quantity < 0:
            return None, "Quantity must be a non-negative whole number."
    except ValueError:
        return None, "Quantity must be a non-negative whole number."

    try:
        price = float(price_text)
        if price < 0 or not math.isfinite(price):
            return None, "Unit price must be a valid non-negative number."
    except ValueError:
        return None, "Unit price must be a valid non-negative number."

    try:
        reorder_level = int(reorder_text)
        if reorder_level < 0:
            raise ValueError
    except ValueError:
        return None, "Reorder level must be a non-negative whole number."
    try:
        purchase_price = float(purchase_price_text) if purchase_price_text else None
        if purchase_price is not None and (purchase_price < 0 or not math.isfinite(purchase_price)):
            raise ValueError
    except ValueError:
        return None, "Purchase price must be blank or a valid non-negative number."
    preferred_supplier_id = None
    if preferred_supplier_text:
        try:
            preferred_supplier_id = int(preferred_supplier_text)
        except ValueError:
            return None, "Choose a valid preferred supplier."
        with database_connection() as connection:
            supplier_exists = connection.execute(
                "SELECT 1 FROM suppliers WHERE id = ?", (preferred_supplier_id,)
            ).fetchone()
        if not supplier_exists:
            return None, "Choose an existing preferred supplier."
    return {
        "name": name, "quantity": quantity, "price": price, "sku": sku,
        "category": category, "brand": brand, "description": description,
        "unit": unit, "purchase_price": purchase_price,
        "reorder_level": reorder_level, "preferred_supplier_id": preferred_supplier_id,
    }, None


def inventory_totals(products):
    """Calculate the totals shared by the dashboard and reports page."""
    total_units = sum(product["quantity"] for product in products)
    inventory_value = sum(product["quantity"] * product["price"] for product in products)
    zero_stock_count = sum(1 for product in products if product["quantity"] == 0)
    low_stock_count = sum(
        1 for product in products
        if product["quantity"] <= product.get("reorder_level", LOW_STOCK_THRESHOLD)
    )
    return total_units, inventory_value, zero_stock_count, low_stock_count


def validate_supplier_form(form_data, existing_suppliers, ignore_id=None):
    """Check supplier fields and stop two suppliers using the same name."""
    supplier = {
        "name": form_data.get("name", "").strip(),
        "contact_person": form_data.get("contact_person", "").strip(),
        "phone": form_data.get("phone", "").strip(),
        "email": form_data.get("email", "").strip(),
        "address": form_data.get("address", "").strip(),
    }
    if not all(supplier.values()):
        return None, "Please complete every supplier field."
    if "@" not in supplier["email"] or "." not in supplier["email"].split("@")[-1]:
        return None, "Enter a valid email address."
    for existing in existing_suppliers:
        if existing["id"] != ignore_id and existing["name"].casefold() == supplier["name"].casefold():
            return None, "A supplier with that name already exists."
    return supplier, None


def get_suppliers():
    """Return suppliers in name order from SQLite."""
    with database_connection() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM suppliers ORDER BY name")]


def validate_customer_form(form_data):
    """Normalize and validate the required customer contact fields."""
    customer = {
        "name": form_data.get("name", "").strip(),
        "business_name": form_data.get("business_name", "").strip(),
        "phone": form_data.get("phone", "").strip(),
        "email": form_data.get("email", "").strip(),
        "address": form_data.get("address", "").strip(),
    }
    required = (customer["name"], customer["phone"], customer["email"], customer["address"])
    if not all(required):
        return None, "Customer name, phone, email, and address are required."
    phone_digits = re.sub(r"\D", "", customer["phone"])
    if not re.fullmatch(r"\+?[0-9][0-9\s().-]{5,19}", customer["phone"]) or not 7 <= len(phone_digits) <= 15:
        return None, "Enter a valid phone number with 7 to 15 digits."
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", customer["email"]):
        return None, "Enter a valid email address."
    return customer, None


def get_customers():
    initialize_database()
    with database_connection() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM customers ORDER BY name")]


def purchase_form_context(form_data=None):
    """Prepare suppliers and products for the purchase entry form."""
    return {
        "suppliers": get_suppliers(),
        "products": load_products(),
        "form_data": form_data or {},
    }


def sale_form_context(form_data=None):
    return {
        "customers": get_customers(),
        "products": load_products(),
        "form_data": form_data or {},
        "today": date.today().isoformat(),
    }


@app.route("/")
def dashboard():
    products = load_products()

    total_products = len(products)
    total_units, inventory_value, _, low_stock_count = inventory_totals(products)

    return render_template(
        "dashboard.html",
        products=products,
        total_products=total_products,
        total_units=total_units,
        inventory_value=inventory_value,
        low_stock_count=low_stock_count,
    )


@app.route("/products")
def products_page():
    """Show saved products, optionally filtered by a name search."""
    all_products = load_products()
    search_text = request.args.get("q", "").strip()

    indexed_products = list(enumerate(all_products))
    if search_text:
        # casefold() makes the name comparison ignore uppercase/lowercase.
        matching_products = [
            (index, product)
            for index, product in indexed_products
            if any(search_text.casefold() in str(product.get(field, "")).casefold()
                   for field in ("name", "sku", "category", "brand"))
        ]
    else:
        matching_products = indexed_products

    return render_template(
        "products.html",
        products=matching_products,
        search_text=search_text,
        has_saved_products=bool(all_products),
    )


@app.route("/products/add", methods=["GET", "POST"])
def add_product_page():
    """Display the add form and save a valid product submitted from it."""
    if request.method == "POST":
        products = load_products()
        new_product, error = validate_product_form(request.form, products)
        if error:
            flash(error, "danger")
            return render_template("product_form.html", form_data=request.form, suppliers=get_suppliers())

        products.append(new_product)

        if save_products(products):
            flash(f"{new_product['name']} was added successfully.", "success")
            return redirect(url_for("products_page"))

        # Remove the unsaved product from memory if the file could not be written.
        products.pop()
        flash("The product could not be saved. Please try again.", "danger")
        return render_template("product_form.html", form_data=request.form, suppliers=get_suppliers()), 500

    return render_template("product_form.html", form_data={}, suppliers=get_suppliers())


@app.route("/products/<int:product_index>/edit", methods=["GET", "POST"])
def edit_product_page(product_index):
    """Display and process an edit form for one saved product."""
    products = load_products()
    if product_index < 0 or product_index >= len(products):
        flash("That product could not be found.", "danger")
        return redirect(url_for("products_page"))

    current_product = products[product_index]
    if request.method == "POST":
        updated_product, error = validate_product_form(
            request.form, products, ignore_index=product_index
        )
        if error:
            flash(error, "danger")
            return render_template(
                "product_form.html",
                form_data=request.form,
                form_title="Edit Product",
                submit_label="Save Changes",
                form_action=url_for("edit_product_page", product_index=product_index),
                suppliers=get_suppliers(),
            )

        old_product = products[product_index]
        updated_product["id"] = old_product["id"]
        # Keep the SQLite row ID and any extra fields from the saved record.
        for key, value in old_product.items():
            if key not in PRODUCT_FIELDS and key != "id":
                updated_product[key] = value
        products[product_index] = updated_product
        if save_products(products):
            flash(f"{updated_product['name']} was updated successfully.", "success")
            return redirect(url_for("products_page"))

        products[product_index] = old_product
        flash("The product could not be saved. Please try again.", "danger")
        return render_template(
            "product_form.html",
            form_data=request.form,
            form_title="Edit Product",
            submit_label="Save Changes",
            form_action=url_for("edit_product_page", product_index=product_index),
            suppliers=get_suppliers(),
        ), 500

    return render_template(
        "product_form.html",
        form_data=current_product,
        suppliers=get_suppliers(),
        form_title="Edit Product",
        submit_label="Save Changes",
        form_action=url_for("edit_product_page", product_index=product_index),
    )


@app.route("/products/<int:product_index>/delete", methods=["POST"])
def delete_product_page(product_index):
    """Delete a product only after the Products page confirms the action."""
    products = load_products()
    if product_index < 0 or product_index >= len(products):
        flash("That product could not be found.", "danger")
        return redirect(url_for("products_page"))

    deleted_product = products.pop(product_index)
    if save_products(products):
        flash(f"{deleted_product['name']} was deleted.", "success")
    else:
        products.insert(product_index, deleted_product)
        flash("The product could not be deleted. Please try again.", "danger")
    return redirect(url_for("products_page"))


def stock_movement_page(movement_type):
    """Display a stock movement form and save a validated movement."""
    products = load_products()
    template_values = {
        "products": list(enumerate(products)),
        "movement_type": movement_type,
        "form_data": request.form if request.method == "POST" else {},
    }

    if request.method == "POST":
        try:
            product_index = int(request.form.get("product_index", ""))
            if product_index < 0 or product_index >= len(products):
                raise ValueError
        except ValueError:
            flash("Please select a valid product.", "danger")
            return render_template("stock_form.html", **template_values)

        try:
            quantity_moved = int(request.form.get("quantity", ""))
            if quantity_moved <= 0:
                raise ValueError
        except ValueError:
            flash("Quantity must be a positive whole number.", "danger")
            return render_template("stock_form.html", **template_values)

        product = products[product_index]
        previous_quantity = product["quantity"]
        if movement_type == "out" and quantity_moved > previous_quantity:
            flash(
                f"Insufficient stock. Only {previous_quantity} unit(s) are available.",
                "danger",
            )
            return render_template("stock_form.html", **template_values)

        try:
            transactions = load_transactions()
        except (OSError, json.JSONDecodeError, ValueError) as error:
            flash(f"Transaction history could not be read: {error}", "danger")
            return render_template("stock_form.html", **template_values)

        if movement_type == "in":
            product["quantity"] += quantity_moved
            transaction_type = "Stock In"
        else:
            product["quantity"] -= quantity_moved
            transaction_type = "Stock Out"

        transaction = {
            "transaction_type": transaction_type,
            "product_name": product["name"],
            "quantity": quantity_moved,
            "date_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stock_after": product["quantity"],
        }

        if not save_products(products):
            flash("Stock could not be saved. No transaction was recorded.", "danger")
            return render_template("stock_form.html", **template_values)

        transactions.append(transaction)
        if not save_transactions(transactions):
            # Try to restore the stock so an unrecorded movement is not kept.
            product["quantity"] = previous_quantity
            if save_products(products):
                flash("Transaction history could not be saved; stock was restored.", "danger")
            else:
                flash(
                    "Transaction history and stock could not both be saved. "
                    "Please check the JSON files.",
                    "danger",
                )
            return render_template("stock_form.html", **template_values)

        flash(
            f"{transaction_type} recorded for {product['name']}: "
            f"{previous_quantity} to {product['quantity']} units.",
            "success",
        )
        return redirect(url_for("stock_in_page" if movement_type == "in" else "stock_out_page"))

    return render_template("stock_form.html", **template_values)


@app.route("/stock-in", methods=["GET", "POST"])
def stock_in_page():
    """Handle incoming stock from the website."""
    return stock_movement_page("in")


@app.route("/stock-out", methods=["GET", "POST"])
def stock_out_page():
    """Handle outgoing stock from the website."""
    return stock_movement_page("out")


@app.route("/transactions")
def transactions_page():
    """Show saved stock movement history."""
    try:
        transactions = load_transactions()
    except (OSError, json.JSONDecodeError, ValueError) as error:
        flash(f"Transaction history could not be read: {error}", "danger")
        transactions = []
    return render_template("transactions.html", transactions=list(reversed(transactions)))


@app.route("/low-stock")
def low_stock_page():
    """Show products at or below the low-stock threshold."""
    products = [
        product for product in load_products()
        if product["quantity"] <= product.get("reorder_level", LOW_STOCK_THRESHOLD)
    ]
    return render_template(
        "low_stock.html",
        products=products,
        threshold=LOW_STOCK_THRESHOLD,
    )


@app.route("/reports")
def reports_page():
    """Show inventory totals calculated from current saved product data."""
    products = load_products()
    total_units, inventory_value, zero_stock_count, low_stock_count = inventory_totals(products)
    return render_template(
        "reports.html",
        total_products=len(products),
        total_units=total_units,
        inventory_value=inventory_value,
        zero_stock_count=zero_stock_count,
        low_stock_count=low_stock_count,
    )


@app.route("/suppliers")
def suppliers_page():
    """Show suppliers, with an optional case-insensitive name search."""
    search_text = request.args.get("q", "").strip()
    initialize_database()
    with database_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM suppliers WHERE name LIKE ? COLLATE NOCASE ORDER BY name",
            (f"%{search_text}%",),
        ).fetchall()
    return render_template("suppliers.html", suppliers=rows, search_text=search_text)


@app.route("/suppliers/add", methods=["GET", "POST"])
def add_supplier_page():
    """Display the supplier form and save a valid new supplier."""
    if request.method == "POST":
        supplier, error = validate_supplier_form(request.form, get_suppliers())
        if error:
            flash(error, "danger")
            return render_template("supplier_form.html", form_data=request.form)
        try:
            with database_connection() as connection:
                connection.execute(
                    """INSERT INTO suppliers
                       (name, contact_person, phone, email, address)
                       VALUES (?, ?, ?, ?, ?)""",
                    tuple(supplier[key] for key in ("name", "contact_person", "phone", "email", "address")),
                )
        except sqlite3.IntegrityError:
            flash("A supplier with that name already exists.", "danger")
            return render_template("supplier_form.html", form_data=request.form)
        flash(f"Supplier {supplier['name']} was added.", "success")
        return redirect(url_for("suppliers_page"))
    return render_template("supplier_form.html", form_data={})


@app.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
def edit_supplier_page(supplier_id):
    """Edit the contact details for an existing supplier."""
    initialize_database()
    with database_connection() as connection:
        row = connection.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if row is None:
        flash("That supplier could not be found.", "danger")
        return redirect(url_for("suppliers_page"))
    current = dict(row)
    if request.method == "POST":
        supplier, error = validate_supplier_form(request.form, get_suppliers(), supplier_id)
        if error:
            flash(error, "danger")
            return render_template(
                "supplier_form.html", form_data=request.form,
                form_title="Edit Supplier", submit_label="Save changes",
                form_action=url_for("edit_supplier_page", supplier_id=supplier_id),
            )
        try:
            with database_connection() as connection:
                connection.execute(
                    """UPDATE suppliers SET name = ?, contact_person = ?, phone = ?, email = ?, address = ?
                       WHERE id = ?""",
                    (*[supplier[key] for key in ("name", "contact_person", "phone", "email", "address")], supplier_id),
                )
        except sqlite3.IntegrityError:
            flash("A supplier with that name already exists.", "danger")
            return render_template(
                "supplier_form.html", form_data=request.form,
                form_title="Edit Supplier", submit_label="Save changes",
                form_action=url_for("edit_supplier_page", supplier_id=supplier_id),
            )
        flash(f"Supplier {supplier['name']} was updated.", "success")
        return redirect(url_for("suppliers_page"))
    return render_template(
        "supplier_form.html", form_data=current,
        form_title="Edit Supplier", submit_label="Save changes",
        form_action=url_for("edit_supplier_page", supplier_id=supplier_id),
    )


@app.route("/suppliers/<int:supplier_id>/delete", methods=["POST"])
def delete_supplier_page(supplier_id):
    """Delete a supplier only when it has no purchase records."""
    initialize_database()
    try:
        with database_connection() as connection:
            linked = connection.execute(
                "SELECT COUNT(*) FROM purchases WHERE supplier_id = ?", (supplier_id,)
            ).fetchone()[0]
            if linked:
                flash("This supplier has purchase records and cannot be deleted.", "danger")
            else:
                deleted = connection.execute(
                    "DELETE FROM suppliers WHERE id = ?", (supplier_id,)
                ).rowcount
                flash("Supplier deleted." if deleted else "Supplier not found.", "success" if deleted else "danger")
    except sqlite3.IntegrityError:
        flash("This supplier is linked to purchase records and cannot be deleted.", "danger")
    return redirect(url_for("suppliers_page"))


@app.route("/purchases")
def purchases_page():
    """Show purchase invoices, newest purchase dates first."""
    initialize_database()
    with database_connection() as connection:
        purchases = connection.execute(
            """SELECT p.*, s.name AS supplier_name FROM purchases AS p
               JOIN suppliers AS s ON s.id = p.supplier_id
               ORDER BY p.purchase_date DESC, p.id DESC"""
        ).fetchall()
        purchase_items = {}
        purchases_with_payments = []
        for purchase in purchases:
            purchase_items[purchase["id"]] = connection.execute(
                """SELECT product_name, quantity FROM purchase_items
                   WHERE purchase_id = ? ORDER BY id""", (purchase["id"],)
            ).fetchall()
            enriched, _ = enrich_invoice_payment(
                connection, purchase, "supplier_payments", "purchase_id"
            )
            purchases_with_payments.append(enriched)
    return render_template("purchases.html", purchases=purchases_with_payments, purchase_items=purchase_items)


@app.route("/purchases/add", methods=["GET", "POST"])
def add_purchase_page():
    """Validate a supplier invoice and atomically save it with stock updates."""
    initialize_database()
    if request.method == "GET":
        return render_template("purchase_form.html", **purchase_form_context())

    form_data = request.form
    error = None
    saved_purchase_id = None
    try:
        supplier_id = int(form_data.get("supplier_id", ""))
        invoice_reference = form_data.get("invoice_reference", "").strip()
        purchase_date = form_data.get("purchase_date", "").strip()
        datetime.strptime(purchase_date, "%Y-%m-%d")
        due_date = optional_iso_date(form_data.get("due_date"), "due date")
        if not invoice_reference:
            raise ValueError("Invoice/reference number is required.")

        product_ids = form_data.getlist("product_id")
        quantities = form_data.getlist("quantity")
        prices = form_data.getlist("unit_price")
        if not (len(product_ids) == len(quantities) == len(prices)):
            raise ValueError("Purchase lines are incomplete. Please review the form.")
        items = []
        for row_number, (product_text, quantity_text, price_text) in enumerate(
            zip(product_ids, quantities, prices), start=1
        ):
            if not product_text and not quantity_text and not price_text:
                continue
            if not product_text:
                raise ValueError(f"Choose a product for line {row_number}.")
            try:
                product_id = int(product_text)
                quantity = int(quantity_text)
                unit_price = Decimal(price_text)
            except (ValueError, InvalidOperation):
                raise ValueError(f"Line {row_number} needs a valid product, whole quantity, and price.")
            if quantity <= 0:
                raise ValueError("Purchase quantities must be positive whole numbers.")
            if not unit_price.is_finite() or unit_price < 0:
                raise ValueError("Unit prices must be valid non-negative numbers.")
            items.append({
                "product_id": product_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": unit_price * quantity,
            })
        if not items:
            raise ValueError("Add at least one complete purchase line.")

        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM suppliers WHERE id = ?", (supplier_id,)).fetchone() is None:
                raise ValueError("Choose an existing supplier.")
            duplicate = connection.execute(
                "SELECT 1 FROM purchases WHERE supplier_id = ? AND invoice_reference = ? COLLATE NOCASE",
                (supplier_id, invoice_reference),
            ).fetchone()
            if duplicate:
                raise ValueError("That invoice/reference already exists for this supplier.")

            for item in items:
                product = connection.execute(
                    "SELECT id, name, quantity FROM products WHERE id = ?",
                    (item["product_id"],),
                ).fetchone()
                if product is None:
                    raise ValueError("Every purchase line must select an existing product.")
                item["name"] = product["name"]

            invoice_total = sum((item["line_total"] for item in items), Decimal("0"))
            cursor = connection.execute(
                """INSERT INTO purchases
                   (supplier_id, invoice_reference, purchase_date, due_date, total)
                   VALUES (?, ?, ?, ?, ?)""",
                (supplier_id, invoice_reference, purchase_date, due_date, float(invoice_total)),
            )
            saved_purchase_id = cursor.lastrowid
            for item in items:
                connection.execute(
                    "UPDATE products SET quantity = quantity + ? WHERE id = ?",
                    (item["quantity"], item["product_id"]),
                )
                updated_quantity = connection.execute(
                    "SELECT quantity FROM products WHERE id = ?", (item["product_id"],)
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO purchase_items
                       (purchase_id, product_id, product_name, quantity, unit_price, line_total, stock_after)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (saved_purchase_id, item["product_id"], item["name"], item["quantity"],
                     float(item["unit_price"]), float(item["line_total"]), updated_quantity),
                )
        flash(f"Purchase {invoice_reference} recorded. Stock and total were updated.", "success")
        return redirect(url_for("purchase_detail_page", purchase_id=saved_purchase_id))
    except sqlite3.IntegrityError as exception:
        message = str(exception).lower()
        if "unique" in message:
            error = "That invoice/reference already exists for this supplier."
        elif "foreign key" in message:
            error = "Choose an existing supplier and existing products."
        else:
            error = "The purchase contains a value that could not be saved. No stock changes were kept."
    except (ValueError, OSError, sqlite3.Error) as exception:
        error = str(exception) or "The purchase could not be saved. No stock changes were kept."

    flash(error, "danger")
    return render_template("purchase_form.html", **purchase_form_context(form_data))


@app.route("/purchases/<int:purchase_id>")
def purchase_detail_page(purchase_id):
    """Show one saved invoice and all its product lines."""
    initialize_database()
    with database_connection() as connection:
        purchase = connection.execute(
            """SELECT p.*, s.name AS supplier_name FROM purchases AS p
               JOIN suppliers AS s ON s.id = p.supplier_id WHERE p.id = ?""",
            (purchase_id,),
        ).fetchone()
        items = connection.execute(
            "SELECT * FROM purchase_items WHERE purchase_id = ? ORDER BY id", (purchase_id,)
        ).fetchall()
        payment_rows = fetch_invoice_payments(
            connection, "supplier_payments", "purchase_id", purchase_id
        )
    if purchase is None:
        flash("That purchase could not be found.", "danger")
        return redirect(url_for("purchases_page"))
    purchase = dict(purchase)
    purchase.update(payment_state(purchase, payment_rows))
    return render_template(
        "purchase_detail.html", purchase=purchase, items=items,
        payment_history=payment_rows, payment_token=secrets.token_urlsafe(32),
        payment_methods=PAYMENT_METHODS, today=date.today().isoformat(),
    )


@app.route("/purchases/<int:purchase_id>/payments", methods=["POST"])
def add_supplier_payment_page(purchase_id):
    return record_invoice_payment("purchase", purchase_id)


@app.route("/suppliers/<int:supplier_id>/purchases")
def supplier_purchase_history_page(supplier_id):
    """Show all invoices and line details recorded for one supplier."""
    initialize_database()
    with database_connection() as connection:
        supplier = connection.execute(
            "SELECT id, name FROM suppliers WHERE id = ?", (supplier_id,)
        ).fetchone()
        purchases = connection.execute(
            """SELECT * FROM purchases WHERE supplier_id = ?
               ORDER BY purchase_date DESC, id DESC""", (supplier_id,)
        ).fetchall()
        items = {}
        enriched_purchases = []
        for purchase in purchases:
            items[purchase["id"]] = connection.execute(
                """SELECT * FROM purchase_items WHERE purchase_id = ? ORDER BY id""",
                (purchase["id"],),
            ).fetchall()
            enriched, _ = enrich_invoice_payment(
                connection, purchase, "supplier_payments", "purchase_id"
            )
            enriched_purchases.append(enriched)
        payment_history = connection.execute(
            """SELECT sp.*, p.invoice_reference FROM supplier_payments AS sp
               JOIN purchases AS p ON p.id = sp.purchase_id
               WHERE p.supplier_id = ? ORDER BY sp.payment_date DESC, sp.id DESC""",
            (supplier_id,),
        ).fetchall()
    if supplier is None:
        flash("That supplier could not be found.", "danger")
        return redirect(url_for("suppliers_page"))
    outstanding_total = sum((purchase["outstanding"] for purchase in enriched_purchases), 0.0)
    return render_template(
        "supplier_purchase_history.html", supplier=supplier, purchases=enriched_purchases,
        items=items, payment_history=payment_history, outstanding_total=outstanding_total,
    )


@app.route("/customers")
def customers_page():
    """List customers with a case-insensitive contact and business search."""
    search_text = request.args.get("q", "").strip()
    initialize_database()
    with database_connection() as connection:
        customers = connection.execute(
            """SELECT * FROM customers
               WHERE name LIKE ? COLLATE NOCASE OR business_name LIKE ? COLLATE NOCASE
                  OR phone LIKE ? OR email LIKE ? COLLATE NOCASE
               ORDER BY name""",
            tuple([f"%{search_text}%"] * 4),
        ).fetchall()
    return render_template("customers.html", customers=customers, search_text=search_text)


@app.route("/customers/add", methods=["GET", "POST"])
def add_customer_page():
    initialize_database()
    if request.method == "POST":
        customer, error = validate_customer_form(request.form)
        if error:
            flash(error, "danger")
            return render_template("customer_form.html", form_data=request.form)
        try:
            with database_connection() as connection:
                connection.execute(
                    """INSERT INTO customers (name, business_name, phone, email, address)
                       VALUES (?, ?, ?, ?, ?)""",
                    (customer["name"], customer["business_name"], customer["phone"],
                     customer["email"], customer["address"]),
                )
        except sqlite3.IntegrityError:
            flash("A customer with that phone number or email already exists.", "danger")
            return render_template("customer_form.html", form_data=request.form)
        flash(f"Customer {customer['name']} was added.", "success")
        return redirect(url_for("customers_page"))
    return render_template("customer_form.html", form_data={})


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
def edit_customer_page(customer_id):
    initialize_database()
    with database_connection() as connection:
        row = connection.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        flash("That customer could not be found.", "danger")
        return redirect(url_for("customers_page"))
    if request.method == "POST":
        customer, error = validate_customer_form(request.form)
        if error:
            flash(error, "danger")
            return render_template(
                "customer_form.html", form_data=request.form,
                form_title="Edit Customer", submit_label="Save changes",
                form_action=url_for("edit_customer_page", customer_id=customer_id),
            )
        try:
            with database_connection() as connection:
                connection.execute(
                    """UPDATE customers SET name = ?, business_name = ?, phone = ?, email = ?, address = ?
                       WHERE id = ?""",
                    (customer["name"], customer["business_name"], customer["phone"],
                     customer["email"], customer["address"], customer_id),
                )
        except sqlite3.IntegrityError:
            flash("Another customer already uses that phone number or email.", "danger")
            return render_template(
                "customer_form.html", form_data=request.form,
                form_title="Edit Customer", submit_label="Save changes",
                form_action=url_for("edit_customer_page", customer_id=customer_id),
            )
        flash(f"Customer {customer['name']} was updated.", "success")
        return redirect(url_for("customers_page"))
    return render_template(
        "customer_form.html", form_data=dict(row),
        form_title="Edit Customer", submit_label="Save changes",
        form_action=url_for("edit_customer_page", customer_id=customer_id),
    )


@app.route("/customers/<int:customer_id>/delete", methods=["POST"])
def delete_customer_page(customer_id):
    initialize_database()
    try:
        with database_connection() as connection:
            sales_count = connection.execute(
                "SELECT COUNT(*) FROM sales WHERE customer_id = ?", (customer_id,)
            ).fetchone()[0]
            if sales_count:
                flash("This customer cannot be deleted because sales records refer to them.", "danger")
            else:
                deleted = connection.execute(
                    "DELETE FROM customers WHERE id = ?", (customer_id,)
                ).rowcount
                flash("Customer deleted." if deleted else "Customer not found.", "success" if deleted else "danger")
    except sqlite3.IntegrityError:
        flash("This customer has sales records and cannot be deleted.", "danger")
    return redirect(url_for("customers_page"))


@app.route("/sales")
def sales_page():
    initialize_database()
    search_text = request.args.get("q", "").strip()
    customer_filter = request.args.get("customer_id", "").strip()
    date_from_text = request.args.get("date_from", "").strip()
    date_to_text = request.args.get("date_to", "").strip()
    filters = []
    parameters = []
    if search_text:
        filters.append(
            "(s.invoice_reference LIKE ? COLLATE NOCASE OR c.name LIKE ? COLLATE NOCASE "
            "OR c.business_name LIKE ? COLLATE NOCASE OR c.phone LIKE ? OR c.email LIKE ? COLLATE NOCASE)"
        )
        parameters.extend([f"%{search_text}%"] * 5)
    if customer_filter:
        if customer_filter.isdigit():
            filters.append("s.customer_id = ?")
            parameters.append(int(customer_filter))
        else:
            flash("Choose a valid customer filter.", "warning")
            customer_filter = ""
    try:
        date_from = optional_iso_date(date_from_text, "start date")
        date_to = optional_iso_date(date_to_text, "end date")
    except ValueError as error:
        flash(str(error), "warning")
        date_from = date_to = None
        date_from_text = date_to_text = ""
    if date_from and date_to and date_from > date_to:
        flash("The start date must not be after the end date.", "warning")
        date_from = date_to = None
        date_from_text = date_to_text = ""
    if date_from:
        filters.append("s.sale_date >= ?")
        parameters.append(date_from)
    if date_to:
        filters.append("s.sale_date <= ?")
        parameters.append(date_to)
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    with database_connection() as connection:
        sales = connection.execute(
            f"""SELECT s.*, c.name AS customer_name FROM sales AS s
               JOIN customers AS c ON c.id = s.customer_id
               {where_sql} ORDER BY s.sale_date DESC, s.id DESC""",
            parameters,
        ).fetchall()
        sale_items = {}
        sales_with_payments = []
        for sale in sales:
            sale_items[sale["id"]] = connection.execute(
                "SELECT product_name, quantity FROM sale_items WHERE sale_id = ? ORDER BY id",
                (sale["id"],),
            ).fetchall()
            enriched, _ = enrich_invoice_payment(connection, sale, "customer_payments", "sale_id")
            sales_with_payments.append(enriched)
    return render_template(
        "sales.html", sales=sales_with_payments, sale_items=sale_items,
        customers=get_customers(), search_text=search_text, customer_filter=customer_filter,
        date_from=date_from_text, date_to=date_to_text,
    )


@app.route("/sales/add", methods=["GET", "POST"])
def add_sale_page():
    initialize_database()
    if request.method == "GET":
        return render_template("sale_form.html", **sale_form_context())

    form_data = request.form
    error = None
    transaction_history_written = False
    original_transactions = None
    try:
        customer_id = int(form_data.get("customer_id", ""))
        invoice_reference = form_data.get("invoice_reference", "").strip()
        sale_date = form_data.get("sale_date", "").strip()
        datetime.strptime(sale_date, "%Y-%m-%d")
        due_date = optional_iso_date(form_data.get("due_date"), "due date")
        product_ids = form_data.getlist("product_id")
        quantities = form_data.getlist("quantity")
        prices = form_data.getlist("unit_price")
        if not (len(product_ids) == len(quantities) == len(prices)):
            raise ValueError("Sale lines are incomplete. Please review the form.")
        items = []
        for line_no, (product_text, quantity_text, price_text) in enumerate(
            zip(product_ids, quantities, prices), start=1
        ):
            if not product_text and not quantity_text and not price_text:
                continue
            if not product_text:
                raise ValueError(f"Choose a product for line {line_no}.")
            try:
                product_id = int(product_text)
                quantity = int(quantity_text)
                unit_price = Decimal(price_text)
            except (ValueError, InvalidOperation):
                raise ValueError(f"Line {line_no} needs a valid product, whole quantity, and price.")
            if quantity <= 0:
                raise ValueError("Sale quantities must be positive whole numbers.")
            if not unit_price.is_finite() or unit_price < 0 or not math.isfinite(float(unit_price)):
                raise ValueError("Selling prices must be valid non-negative numbers.")
            items.append({
                "product_id": product_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": unit_price * quantity,
            })
        if not items:
            raise ValueError("Add at least one complete sale line.")

        original_transactions = load_transactions()
        sale_time = datetime.now().astimezone().isoformat(timespec="seconds")
        transaction_rows = []
        with database_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone() is None:
                raise ValueError("Choose an existing customer.")
            if not invoice_reference:
                for _ in range(10):
                    invoice_reference = f"SALE-{date.today():%Y%m%d}-{secrets.token_hex(3).upper()}"
                    if connection.execute(
                        "SELECT 1 FROM sales WHERE invoice_reference = ? COLLATE NOCASE",
                        (invoice_reference,),
                    ).fetchone() is None:
                        break
                else:
                    raise ValueError("An invoice number could not be generated. Please try again.")
            if connection.execute(
                "SELECT 1 FROM sales WHERE invoice_reference = ? COLLATE NOCASE",
                (invoice_reference,),
            ).fetchone():
                raise ValueError("That invoice/reference has already been used.")

            product_info = {}
            requested_by_product = {}
            for item in items:
                product = connection.execute(
                    "SELECT id, name, quantity FROM products WHERE id = ?", (item["product_id"],)
                ).fetchone()
                if product is None:
                    raise ValueError("Every sale line must select an existing product.")
                product_info[item["product_id"]] = product
                requested_by_product[item["product_id"]] = (
                    requested_by_product.get(item["product_id"], 0) + item["quantity"]
                )
                item["name"] = product["name"]
            for product_id, requested in requested_by_product.items():
                available = product_info[product_id]["quantity"]
                if requested > available:
                    raise ValueError(
                        f"Insufficient stock for {product_info[product_id]['name']}. "
                        f"Only {available} unit(s) are available."
                    )

            invoice_total = sum((item["line_total"] for item in items), Decimal("0"))
            if not invoice_total.is_finite() or not math.isfinite(float(invoice_total)):
                raise ValueError("Sale total is too large to save.")
            cursor = connection.execute(
                """INSERT INTO sales
                   (customer_id, invoice_reference, sale_date, due_date, total)
                   VALUES (?, ?, ?, ?, ?)""",
                (customer_id, invoice_reference, sale_date, due_date, float(invoice_total)),
            )
            sale_id = cursor.lastrowid
            for item in items:
                updated = connection.execute(
                    """UPDATE products SET quantity = quantity - ?
                       WHERE id = ? AND quantity >= ?""",
                    (item["quantity"], item["product_id"], item["quantity"]),
                )
                if updated.rowcount != 1:
                    raise ValueError(f"Insufficient stock for {item['name']}. The sale was not saved.")
                stock_after = connection.execute(
                    "SELECT quantity FROM products WHERE id = ?", (item["product_id"],)
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO sale_items
                       (sale_id, product_id, product_name, quantity, unit_price, line_total, stock_after)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (sale_id, item["product_id"], item["name"], item["quantity"],
                     float(item["unit_price"]), float(item["line_total"]), stock_after),
                )
                transaction_rows.append({
                    "transaction_type": "Stock Out",
                    "product_name": item["name"],
                    "quantity": item["quantity"],
                    "date_time": sale_time,
                    "stock_after": stock_after,
                    "invoice_reference": invoice_reference,
                })

            if not save_transactions(original_transactions + transaction_rows):
                raise OSError("Transaction history could not be saved; the sale was rolled back.")
            transaction_history_written = True

        flash(f"Sale {invoice_reference} recorded and stock updated.", "success")
        return redirect(url_for("sale_detail_page", sale_id=sale_id))
    except sqlite3.IntegrityError as exception:
        if transaction_history_written and original_transactions is not None:
            save_transactions(original_transactions)
        message = str(exception).lower()
        error = (
            "That invoice/reference has already been used."
            if "unique" in message else
            "The sale could not be saved. No stock changes were kept."
        )
    except (ValueError, OSError, sqlite3.Error) as exception:
        if transaction_history_written and original_transactions is not None:
            save_transactions(original_transactions)
        error = str(exception) or "The sale could not be saved. No stock changes were kept."
    flash(error, "danger")
    return render_template("sale_form.html", **sale_form_context(form_data))


@app.route("/sales/<int:sale_id>")
def sale_detail_page(sale_id):
    initialize_database()
    with database_connection() as connection:
        sale = connection.execute(
            """SELECT s.*, c.name AS customer_name FROM sales AS s
               JOIN customers AS c ON c.id = s.customer_id WHERE s.id = ?""", (sale_id,)
        ).fetchone()
        items = connection.execute(
            "SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id", (sale_id,)
        ).fetchall()
        payment_rows = fetch_invoice_payments(
            connection, "customer_payments", "sale_id", sale_id
        )
    if sale is None:
        flash("That sale could not be found.", "danger")
        return redirect(url_for("sales_page"))
    sale = dict(sale)
    sale.update(payment_state(sale, payment_rows))
    return render_template(
        "sale_detail.html", sale=sale, items=items,
        payment_history=payment_rows, payment_token=secrets.token_urlsafe(32),
        payment_methods=PAYMENT_METHODS, today=date.today().isoformat(),
    )


@app.route("/sales/<int:sale_id>/payments", methods=["POST"])
def add_customer_payment_page(sale_id):
    return record_invoice_payment("sale", sale_id)


@app.route("/customers/<int:customer_id>/sales")
def customer_sales_history_page(customer_id):
    initialize_database()
    with database_connection() as connection:
        customer = connection.execute(
            "SELECT id, name, business_name FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        sales = connection.execute(
            "SELECT * FROM sales WHERE customer_id = ? ORDER BY sale_date DESC, id DESC", (customer_id,)
        ).fetchall()
        items = {
            sale["id"]: connection.execute(
                "SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id", (sale["id"],)
            ).fetchall() for sale in sales
        }
        enriched_sales = []
        for sale in sales:
            enriched, _ = enrich_invoice_payment(connection, sale, "customer_payments", "sale_id")
            enriched_sales.append(enriched)
        payment_history = connection.execute(
            """SELECT cp.*, s.invoice_reference FROM customer_payments AS cp
               JOIN sales AS s ON s.id = cp.sale_id
               WHERE s.customer_id = ? ORDER BY cp.payment_date DESC, cp.id DESC""",
            (customer_id,),
        ).fetchall()
    if customer is None:
        flash("That customer could not be found.", "danger")
        return redirect(url_for("customers_page"))
    return render_template(
        "customer_sales_history.html", customer=customer, sales=enriched_sales, items=items,
        payment_history=payment_history,
        outstanding_total=sum((sale["outstanding"] for sale in enriched_sales), 0.0),
    )


def payment_register_data():
    """Build the filtered payment register shared by the page and CSV export."""
    initialize_database()
    kind_filter = request.args.get("kind", "all")
    status_filter = request.args.get("status", "all")
    party_filter = request.args.get("party", "").strip().casefold()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    for label, value in (("start date", date_from), ("end date", date_to)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                flash(f"Enter a valid {label} filter.", "warning")
                if label == "start date":
                    date_from = ""
                else:
                    date_to = ""

    with database_connection() as connection:
        purchase_rows = connection.execute(
            """SELECT p.*, s.name AS party_name FROM purchases AS p
               JOIN suppliers AS s ON s.id = p.supplier_id"""
        ).fetchall()
        sale_rows = connection.execute(
            """SELECT s.*, c.name AS party_name FROM sales AS s
               JOIN customers AS c ON c.id = s.customer_id"""
        ).fetchall()
        invoices = []
        for invoice in purchase_rows:
            enriched, _ = enrich_invoice_payment(
                connection, invoice, "supplier_payments", "purchase_id"
            )
            enriched.update({"invoice_kind": "purchase", "invoice_date": invoice["purchase_date"]})
            invoices.append(enriched)
        for invoice in sale_rows:
            enriched, _ = enrich_invoice_payment(
                connection, invoice, "customer_payments", "sale_id"
            )
            enriched.update({"invoice_kind": "sale", "invoice_date": invoice["sale_date"]})
            invoices.append(enriched)

    receivables = sum((invoice["outstanding"] for invoice in invoices if invoice["invoice_kind"] == "sale"), 0.0)
    payables = sum((invoice["outstanding"] for invoice in invoices if invoice["invoice_kind"] == "purchase"), 0.0)
    partial_count = sum(invoice["payment_status"] == "Partially Paid" for invoice in invoices)
    unpaid_count = sum(invoice["payment_status"] == "Unpaid" for invoice in invoices)
    overdue_count = sum(invoice["is_overdue"] for invoice in invoices)

    filtered = []
    for invoice in invoices:
        if kind_filter in ("purchase", "sale") and invoice["invoice_kind"] != kind_filter:
            continue
        if status_filter == "Overdue":
            if not invoice["is_overdue"]:
                continue
        elif status_filter in ("Unpaid", "Partially Paid", "Paid"):
            if invoice["payment_status"] != status_filter:
                continue
        elif status_filter != "all":
            continue
        if party_filter and party_filter not in invoice["party_name"].casefold():
            continue
        if date_from and invoice["invoice_date"] < date_from:
            continue
        if date_to and invoice["invoice_date"] > date_to:
            continue
        filtered.append(invoice)
    filtered.sort(key=lambda row: (row["invoice_date"], row["id"]), reverse=True)
    return {
        "invoices": filtered,
        "receivables": receivables,
        "payables": payables,
        "partial_count": partial_count,
        "unpaid_count": unpaid_count,
        "overdue_count": overdue_count,
        "kind_filter": kind_filter,
        "status_filter": status_filter,
        "party_filter": request.args.get("party", ""),
        "date_from": date_from,
        "date_to": date_to,
    }


@app.route("/payments")
def payments_dashboard_page():
    """Show live receivables/payables and filter invoice balances."""
    return render_template("payments.html", **payment_register_data())


def spreadsheet_safe_text(value):
    """Keep untrusted text from being interpreted as a spreadsheet formula."""
    text = "" if value is None else str(value)
    if text.lstrip("\ufeff\r\n\t ").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def csv_amount(value):
    """Render a rupee amount as a two-decimal numeric CSV field."""
    return f"{Decimal(str(value)).quantize(Decimal('0.01')):.2f}"


@app.route("/payments/export.csv")
def export_payments_csv():
    """Download the same filtered invoice rows shown in the Payments register."""
    register = payment_register_data()
    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow((
        "Invoice Type", "Invoice Number", "Supplier/Customer Name", "Invoice Date",
        "Due Date", "Invoice Total (INR)", "Amount Paid (INR)",
        "Outstanding Balance (INR)", "Payment Status",
    ))
    for invoice in register["invoices"]:
        writer.writerow((
            "Sale" if invoice["invoice_kind"] == "sale" else "Purchase",
            spreadsheet_safe_text(invoice["invoice_reference"]),
            spreadsheet_safe_text(invoice["party_name"]),
            spreadsheet_safe_text(invoice["invoice_date"]),
            spreadsheet_safe_text(invoice["due_date"]),
            csv_amount(invoice["total"]),
            csv_amount(invoice["amount_paid"]),
            csv_amount(invoice["outstanding"]),
            spreadsheet_safe_text(
                "Overdue" if invoice["is_overdue"] else invoice["payment_status"]
            ),
        ))

    filename = f"payments_register_{date.today().isoformat()}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/products/<int:product_id>/sales")
def product_sales_history_page(product_id):
    initialize_database()
    with database_connection() as connection:
        product = connection.execute(
            "SELECT id, name FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        sales = connection.execute(
            """SELECT si.quantity, si.unit_price, s.id AS sale_id, s.invoice_reference,
                      s.sale_date, c.id AS customer_id, c.name AS customer_name
               FROM sale_items AS si JOIN sales AS s ON s.id = si.sale_id
               JOIN customers AS c ON c.id = s.customer_id
               WHERE si.product_id = ? ORDER BY s.sale_date DESC, s.id DESC, si.id""",
            (product_id,),
        ).fetchall()
    if product is None:
        flash("That product could not be found.", "danger")
        return redirect(url_for("products_page"))
    return render_template("product_sales_history.html", product=product, sales=sales)


@app.route("/products/<int:product_id>/purchases")
def product_purchase_history_page(product_id):
    """Show the supplier invoices and prices for a product's purchases."""
    initialize_database()
    with database_connection() as connection:
        product = connection.execute(
            "SELECT id, name FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        rows = connection.execute(
            """SELECT pi.quantity, pi.unit_price, pi.line_total, p.id AS purchase_id,
                      p.invoice_reference, p.purchase_date, s.id AS supplier_id, s.name AS supplier_name
               FROM purchase_items AS pi
               JOIN purchases AS p ON p.id = pi.purchase_id
               JOIN suppliers AS s ON s.id = p.supplier_id
               WHERE pi.product_id = ?
               ORDER BY p.purchase_date DESC, p.id DESC, pi.id""", (product_id,)
        ).fetchall()
    if product is None:
        flash("That product could not be found.", "danger")
        return redirect(url_for("products_page"))
    return render_template("product_purchase_history.html", product=product, purchases=rows)


if __name__ == "__main__":
    app.run(debug=True)
