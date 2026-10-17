# Stock and Inventory Management System

The project includes a Python console application and a Flask website. Product records are stored in SQLite in `inventory.db`. Stock movement history remains in `transactions.json`.

## First SQLite startup

The first time the application reads products, it creates `inventory.db`, creates the products table, and imports the records from `products.json`. The import runs once and is recorded in SQLite so restarting the app does not create duplicates or restore products that were later deleted.

`products.json` is retained as the original migration source and is not modified by the application. Before migration, copies of the data files were saved under `backups/pre-sqlite-20261017-010348/`.

## Run the Flask website

The website opens directly without login or signup. No `FLASK_SECRET_KEY` or `.env` configuration is required; Flask generates a process-local key for flash messages.

```powershell
.\.venv\Scripts\Activate.ps1
python -m flask --app app run --debug
```

Open the local address printed by Flask, usually `http://127.0.0.1:5000`.

## Run the console application

```powershell
python main.py
```

Both interfaces use the same SQLite product data. The console menu and website Stock In, Stock Out, and Sales movements use the existing transaction history file.

## Record supplier purchases

Open **Purchases** in the sidebar, choose **Record Purchase**, select a supplier, enter the supplier invoice/reference and date, then add one or more product lines. The server calculates line and invoice totals and saves the invoice, its lines, and stock increments in one SQLite transaction. A supplier cannot reuse an invoice/reference number. Open **Purchases** on a supplier row or **Purchase history** on a product row to review the related records.

Purchases are stored in the `purchases` and `purchase_items` tables in `inventory.db`; `purchase_items` references both the purchase and product, while `purchases` references its supplier. The database was backed up before this work under `backups/pre-purchases-hardening-20261017-093048/`.

## Customers and sales

Use **Customers** to add, search, edit, and review customer sales history. Phone numbers and email addresses are unique; a customer with sales cannot be deleted. Under **Sales**, record an invoice with one or more products. The server calculates totals, checks available stock, decreases inventory, saves sale details, and appends Stock Out entries to `transactions.json`. Invoice references are unique across sales. Product rows link to both purchase and sales histories.

The additive schema adds `customers`, `sales`, and `sale_items`; it does not reset existing tables. A pre-change backup of `inventory.db` and `transactions.json` is at `backups/pre-customers-sales-20261017-094333/`.

To test manually, run the Flask command above, add a customer, then record a sale with two products. Check the total on **Sales**, confirm stock on **Products**, and review **Transactions**, the customer's **Sales** history, and each product's **Sales history**. Try a duplicate invoice and a quantity above stock to confirm both are rejected without changing inventory.

## Invoice payments

Open a purchase or sales invoice and use **Record payment** or **Record receipt** for a full or partial payment. Payment rows are append-only in the ordinary UI. Invoice balances and Unpaid, Partially Paid, and Paid status are calculated from the invoice total and saved payment rows. An invoice is overdue only when it has an outstanding balance and a due date earlier than today. Existing invoices were left unpaid with no due date; no prior payment was inferred.

Payments are stored separately in `supplier_payments` and `customer_payments`. The safe additive migration also adds nullable `due_date` columns to `purchases` and `sales`, leaving existing invoice dates and totals intact. The pre-change database and transaction history backup is `backups/pre-payments-20261017-100106/`.

Use **Payments** in the navigation for total customer receivables, supplier payables, partial/unpaid/overdue invoice counts, and filters by invoice type, status, party name, and invoice date range. To verify payment behavior manually, record a partial payment, then the remaining balance; try an amount above the balance and reuse a submission by refreshing/back-posting. Payment actions do not update stock.
