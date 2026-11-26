# Harvest Log · Block 1

The reviewer should be able to open a branded sign-in on a Mac and on Render. The first feature test has an equally concrete outcome. After a manager publishes sampling v2, HL-009 on tank T-04 still displays its v1 steps, and the next sample order displays v2. [Matt’s exercise brief](https://mail.google.com/mail/u/0/#all/1a2149d326128b20) · [Execution plan, pp. 2, 4–7](https://chatgpt.com/library?file_id=libfile_fde3_f-bf57df2c204481918fe4809e05bb871f_ff776c2313b8819194f1b56b12b248ba&account_id=personal)

## Runtime

Use CPython **3.13.18**, Django **5.2.19 LTS**, Django Ninja **1.8.0** and PostgreSQL **16** through **psycopg 3.3.9**. Django renders the HTML and CSS. Gunicorn serves WSGI, WhiteNoise serves collected static files, and dj-database-url reads the database connection string. Both environments use PostgreSQL 16 with the maintenance patch supplied by the host. The Python lock lists all runtime dependencies and their permitted wheel hashes for standard CPython on arm64 and x86-64 macOS and glibc Linux. [Django release](https://docs.djangoproject.com/en/5.2/releases/5.2.19/) · [Django Ninja](https://pypi.org/project/django-ninja/1.8.0/) · [Psycopg wheels](https://pypi.org/project/psycopg-binary/3.3.9/)

## Reviewer’s Mac

Run these commands from the project directory in Bash with Homebrew available. The bootstrap creates a private PostgreSQL 16 cluster under `~/.harvest-log`, waits for startup, and verifies a connection as the application role. The cluster uses a Unix socket with peer authentication. The port number identifies the socket inside its private directory. The server opens no TCP port. [Homebrew PostgreSQL 16](https://formulae.brew.sh/formula/postgresql@16) · [PostgreSQL peer authentication](https://www.postgresql.org/docs/16/auth-peer.html)

```bash
brew install postgresql@16 uv
bash init_mac_postgres.sh
export PGHOST="$HOME/.harvest-log/run" PGPORT=5432
export DATABASE_URL='postgresql://harvest_app@/harvest_log'

uv python install 3.13.18
"$(uv python find 3.13.18)" -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check

if [[ ! -s "$HOME/.harvest-log/django-secret" ]]; then
  (umask 077
   openssl rand -hex 48 > "$HOME/.harvest-log/django-secret")
fi
export DJANGO_SECRET_KEY="$(cat "$HOME/.harvest-log/django-secret")"
export DJANGO_DEBUG=1 DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
```

A successful database check prints `harvest_log`, `harvest_app` and `16.x`. The local app role can create Django’s throwaway test database. In a new shell, activate the virtual environment and repeat the exports above. Rerun `bash init_mac_postgres.sh` to restart the cluster after a reboot. To stop this cluster, run `"$(brew --prefix postgresql@16)/bin/pg_ctl" -D "$HOME/.harvest-log/postgres16" -w stop`.

Once the scaffold and its migrations exist, run `python manage.py migrate --noinput`. When the fictional fixture ships, run `python manage.py seed_demo` twice and check the totals below. Run `python manage.py test` and start the local app with `python manage.py runserver 127.0.0.1:8000`.

## Render

Use one native Python web service and one persistent PostgreSQL 16 database in the same region. Set the start command to `python -m gunicorn config.wsgi:application --bind "0.0.0.0:$PORT" --access-logfile -`. [Render service ports](https://render.com/docs/web-services) · [Internal database URLs](https://render.com/docs/postgresql-creating-connecting)

| Render setting | Value |
| --- | --- |
| Build and pre-deploy commands when both are available | Build with `bash build.sh build`. Pre-deploy with `bash build.sh release`. |
| Build command on a plan without pre-deploy | Use `bash build.sh`. It installs the lock, checks dependencies, collects static assets, then runs the release steps. |
| Runtime and database | Set `PYTHON_VERSION=3.13.18` and `DATABASE_URL` to the managed database’s internal URL. |
| Django environment | Generate a persistent `DJANGO_SECRET_KEY`. Set `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS` to the service hostname, and `DJANGO_CSRF_TRUSTED_ORIGINS` to its full HTTPS origin. |
| Demo seed | Default `SEED_DEMO=0`. Once the command exists, set it to `1` for the first demo release and supply the two demo passwords as host secrets. Restore `0` after success. |

Run migrations once per deployment, in the selected release path. Serialize releases that target the same database. A migration or enabled seed failure must stop the release. The future `seed_demo` command must use stable fictional IDs, create missing records atomically, and preserve existing passwords, revisions, events and work in progress on a repeat run. [Render deploy phases](https://render.com/docs/deploys) · [Python version settings](https://render.com/docs/python-version)

## Authorization contract

Use the authenticated session as the actor. The HTML views and Ninja endpoints must call the same role checks and write functions. Session-authenticated writes require a valid CSRF token.

| Capability | Cellar hand | Cellar manager |
| --- | --- | --- |
| Queue, instructions, readings and timeline | Assigned orders only, including their pinned procedure steps | All orders and their records |
| Issue and assign | Refused | All three work kinds |
| Start, record, complete | Assigned, eligible orders | Any eligible order |
| Standard procedures | Read the revision attached to an assigned order | Read history, edit and publish every kind |
| Winery-wide audit and season report | Refused | Permitted |

## Django scaffold checklist

- [ ] Create the project package `config` for settings, URLs and WSGI. Create one domain app, `cellar`, for models, migrations, write functions, role checks, report queries, views, tests and management commands.
- [ ] Install `django.contrib.contenttypes`, `django.contrib.auth`, `django.contrib.sessions`, `django.contrib.messages`, `django.contrib.staticfiles`, `ninja` and `cellar`. The `ninja` app supplies self-hosted API documentation assets. [Ninja OpenAPI and interactive docs](https://django-ninja.dev/guide/openapi/)
- [ ] Set `AUTH_USER_MODEL = "cellar.User"` **before the first migration**. Create this `AbstractUser` subclass in `cellar.0001_initial`. Use one required, database-checked role per user, with values `hand` and `manager`. Use that field for cellar authorization. [Django custom users](https://docs.djangoproject.com/en/5.2/topics/auth/customizing/)
- [ ] Configure the PostgreSQL backend from a required `DATABASE_URL`, sessions, CSRF and the Django template engine. In the hosted settings, enable secure session and CSRF cookies and recognize HTTPS forwarded by Render’s trusted proxy. Configure `STATIC_ROOT`, WhiteNoise middleware after `SecurityMiddleware`, and the WhiteNoise compressed manifest backend for the `staticfiles` storage alias.
- [ ] Provide the branded sign-in using Alert Enterprise v1.70.0 fonts, logo and CSS. Mount Ninja at `/api/v1/` with `/api/v1/openapi.json` and `/api/v1/docs`. Keep the API schema export with the migrations in version control.

### PostgreSQL table inventory

Use Django’s default table names below. Put the user in the first migration. Scaffold the other model definitions in dependency order for the full-domain migration and fixture in Block 2.

| Check | Table | Required fact or constraint |
| --- | --- | --- |
| ☐ | `cellar_user` | Django identity and exactly one cellar role. Reference this user for assignees and audit actors. |
| ☐ | `cellar_season` | Stable harvest season with a unique season identifier. |
| ☐ | `cellar_lot` | A lot belongs to exactly one season. All order and measurement reports inherit season through the lot. |
| ☐ | `cellar_vessel` | Stable, unique vessel code, such as T-04. |
| ☐ | `cellar_procedure` | One row for each of the three kinds (receive a load, pump over, take a lab sample). Enforce a unique, stable kind. |
| ☐ | `cellar_procedureversion` | Required procedure FK and positive revision number. Unique `(procedure_id, revision)`. Publication seals the revision. |
| ☐ | `cellar_procedurestep` | Version FK, position and trimmed instruction. Unique `(version_id, position)`. At publication require 1–12 consecutive positions and 1–240 characters per step. |
| ☐ | `cellar_workorder` | Unique stable order ID, required lot and assignee, optional vessel and protected non-null procedure-version FK. Constrain status to `not_started`, `in_progress` or `completed`. Derive kind through the pinned version. |
| ☐ | `cellar_receipt` | One-to-one with a receiving order. Net kg is a positive decimal. |
| ☐ | `cellar_pumpoverdetail` | One-to-one with a pump-over order. Elapsed minutes must be positive. |
| ☐ | `cellar_analyte` | Unique analyte code and canonical unit. pH is unitless. Soluble solids are measured in °Brix. |
| ☐ | `cellar_labreading` | Sample-order FK, analyte FK and decimal value. Permit multiple readings per order. The creation event supplies recorder and time. |
| ☐ | `cellar_auditevent` | Append-only actor, server timestamp, action, target and changed fields or revision references. Link each event to its target. Publication and creation events supply attribution once. |

- [ ] On the first `migrate`, verify the migration recorder creates `django_migrations` and the migrations create `django_content_type`, `auth_permission`, `auth_group`, `auth_group_permissions`, `cellar_user_groups`, `cellar_user_user_permissions` and `django_session`. The season report is a query over the domain tables.
- [ ] Protect historical instructions in PostgreSQL. Reject updates or deletion of published revisions, insertion, editing or removal of steps on a sealed revision, changes to procedure kind, and changes to an issued order’s version FK. Use database guards in migrations, including a write-once FK guard in addition to Django’s `PROTECT` deletion behavior. Protect audit rows from updates and deletes.

### First tests and acceptance evidence

- [ ] **Direct role denial.** A signed-in hand posting valid steps and a valid CSRF token to `POST /api/v1/procedures/{kind}/versions` gets `403`. Version, step and audit counts stay fixed. The manager’s equivalent request succeeds. A hand gets `404` for another hand’s order. A signed-out API read gets `401`.
- [ ] **Historical text.** Issue A under v1, publish v2 with a distinct step, issue B, reload both, and complete A. Verify A’s actual step text before and after completion, and verify B’s v2 text. The server chooses the revision on issue.
- [ ] **Atomicity and concurrency.** Issuing and publishing each lock the same procedure row with `select_for_update()` inside `transaction.atomic()`. Check the editor’s base revision under lock. Insert the version, ordered steps and publication event as one transaction. Issue the order and its event as one transaction. Two independent PostgreSQL sessions publishing the same base yield one success, one `409` and one publication event. Force a write failure and check for zero partial rows.
- [ ] **Capture and errors.** Lock the order for transitions and measurement writes. Start, record and complete only when assignment, kind and status allow it. Capture positive kg or minutes for the matching kind. An in-progress sample accepts a registered analyte and decimal result and requires a reading to complete. An illegal transition gets `409`, invalid input gets `422`, and failed writes leave the prior state and history intact.
- [ ] **Season fixture.** Once seeded, 2026 has 12 orders, 8 completed, 4 readings and 6.40 t received. After the demo it has 13, 9, 5 and 6.40 t. Compute orders, completed receipt weights and individual lab readings separately. Verify an empty season, multiple readings on one order, and exclusion of the 900 kg receipt in 2025.
- [ ] **Block 1 exit.** A clean Mac can migrate and open the sign-in. The hosted HTTPS sign-in opens in a fresh browser. Preserve the first real commit and genuine AI exchange, and add the direct-denial and historical-text tests to guide Block 2. During release verification, migrate and seed twice on a clean PostgreSQL database, compare the exported and served OpenAPI schemas, and verify a saved record survives a web-service restart.
