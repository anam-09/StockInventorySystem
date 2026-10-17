import csv
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from app import app


def read_csv_response(response):
    text = response.get_data(as_text=True)
    return list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))


class PaymentsExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        temporary_path = Path(self.temporary_directory.name)
        self.database_patch = patch.object(main, "DATABASE_FILE", temporary_path / "payments-test.sqlite3")
        self.data_patch = patch.object(main, "DATA_FILE", temporary_path / "no-product-migration.json")
        self.database_patch.start()
        self.data_patch.start()
        app.config.update(TESTING=True)
        main.initialize_database()

        with main.database_connection() as connection:
            supplier_id = connection.execute(
                """INSERT INTO suppliers
                   (name, contact_person, phone, email, address)
                   VALUES (?, '', '', '', '')""",
                ('=2+2, "東京商店"',),
            ).lastrowid
            customer_id = connection.execute(
                """INSERT INTO customers (name, phone, email, address)
                   VALUES (?, ?, ?, ?)""",
                ('Zoë, "Market"', "customer-test", "customer@example.test", ""),
            ).lastrowid
            purchase_id = connection.execute(
                """INSERT INTO purchases
                   (supplier_id, invoice_reference, purchase_date, total, due_date)
                   VALUES (?, ?, ?, ?, ?)""",
                (supplier_id, "PUR-001", "2026-01-05", 1000, "2099-01-01"),
            ).lastrowid
            sale_id = connection.execute(
                """INSERT INTO sales
                   (customer_id, invoice_reference, sale_date, total, due_date)
                   VALUES (?, ?, ?, ?, ?)""",
                (customer_id, "SALE,002", "2026-01-10", 500, "2000-01-01"),
            ).lastrowid
            connection.execute(
                """INSERT INTO supplier_payments
                   (purchase_id, payment_date, amount, payment_method, submission_token)
                   VALUES (?, ?, ?, ?, ?)""",
                (purchase_id, "2026-01-06", 250, "UPI", "supplier-payment-test"),
            )
            connection.execute(
                """INSERT INTO customer_payments
                   (sale_id, payment_date, amount, payment_method, submission_token)
                   VALUES (?, ?, ?, ?, ?)""",
                (sale_id, "2026-01-11", 100, "Cash", "customer-payment-test"),
            )

        self.client = app.test_client()

    def tearDown(self):
        self.database_patch.stop()
        self.data_patch.stop()
        self.temporary_directory.cleanup()

    def test_csv_exports_purchase_invoice_and_numeric_amounts(self):
        response = self.client.get("/payments/export.csv")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
        self.assertRegex(
            response.headers["Content-Disposition"],
            r"payments_register_\d{4}-\d{2}-\d{2}\.csv",
        )
        rows = read_csv_response(response)
        self.assertEqual(rows[0], [
            "Invoice Type", "Invoice Number", "Supplier/Customer Name", "Invoice Date",
            "Due Date", "Invoice Total (INR)", "Amount Paid (INR)",
            "Outstanding Balance (INR)", "Payment Status",
        ])
        purchase = next(row for row in rows[1:] if row[0] == "Purchase")
        self.assertEqual(purchase, [
            "Purchase", "PUR-001", "'=2+2, \"東京商店\"", "2026-01-05", "2099-01-01",
            "1000.00", "250.00", "750.00", "Partially Paid",
        ])


    def test_csv_exports_sale_and_overdue_status(self):
        response = self.client.get("/payments/export.csv")
        rows = read_csv_response(response)

        sale = next(row for row in rows[1:] if row[0] == "Sale")
        self.assertEqual(sale, [
            "Sale", "SALE,002", 'Zoë, "Market"', "2026-01-10", "2000-01-01",
            "500.00", "100.00", "400.00", "Overdue",
        ])


    def test_csv_uses_the_payments_register_filters(self):
        response = self.client.get(
            "/payments/export.csv?kind=sale&status=Overdue&party=Zo%C3%AB"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        rows = read_csv_response(response)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "Sale")
        self.assertEqual(rows[1][1], "SALE,002")

        page = self.client.get("/payments?kind=sale&party=Zo%C3%AB")
        self.assertIn(
            b'href="/payments/export.csv?kind=sale&amp;party=Zo%C3%AB"',
            page.data,
        )


    def test_csv_empty_filter_result_has_header_only(self):
        response = self.client.get("/payments/export.csv?kind=purchase&status=Paid")

        rows = read_csv_response(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Invoice Type")
