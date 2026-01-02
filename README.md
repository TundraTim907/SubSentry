# SubSentry

SubSentry helps you find, organize, and control every subscription you’re paying for—without logging into banks, emails, or apps.

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Visit `http://localhost:5000` to create an account, add subscriptions, and export your inventory.

## Deployment notes

- The entrypoint for app servers (Gunicorn, Railway, Render) is `wsgi:app`.
- The local entrypoint is `main.py`, which binds to `0.0.0.0:5000`.

## Trust promise

SubSentry never accesses your bank or email accounts. All data is entered directly by you.
