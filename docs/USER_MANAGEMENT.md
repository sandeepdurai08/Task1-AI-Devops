# BuildBot — User Management

Complete guide for managing who can access BuildBot and what they can do.

---

## How authentication works

BuildBot supports two authentication modes configured in `.env`:

```
AUTH_MODE=jenkins   → users log in with Jenkins credentials (recommended)
AUTH_MODE=local     → users managed in config/users.json only
AUTH_FALLBACK=true  → local admin account always works even if Jenkins is down
```

In Jenkins mode, **Jenkins is the single source of truth**:
- Permissions are read from Jenkins automatically on every login
- No need to duplicate role management in BuildBot
- When Jenkins roles change, BuildBot reflects them at next login

---

## Jenkins mode (recommended)

### Who manages users?

**Jenkins admin** — creates users and assigns roles in Jenkins.
**BuildBot admin** — manages the emergency local admin account only.

### Step 1 — Set up roles in Jenkins

**Manage Jenkins → Manage and Assign Roles → Manage Roles**

#### Recommended global roles

| Role name | Permissions | Suitable for |
|---|---|---|
| `admin-role` | Everything | Jenkins + BuildBot admins |
| `developer-role` | Overall/Read, Job/Build+Read+Cancel, View/Read | Developers who trigger builds |
| `readonly-role` | Overall/Read, Job/Read, View/Read | QA, stakeholders, viewers |

#### Per-job restriction (project role)

To restrict a developer to only specific jobs:

1. In **Manage Roles** → bottom section → add a **Project role**:
   ```
   Role name:  hotfix-only
   Pattern:    hotfix-.*         ← regex matching job names
   Permissions: Job/Build ✅  Job/Read ✅
   ```
2. In **Assign Roles** → assign the developer to `hotfix-only`

> The job pattern is a regex: `hotfix-.*` matches `hotfix-build`, `hotfix-v2`, etc.
> Use `^hotfix-build$` for an exact match.

### Step 2 — Add users in Jenkins

**Manage Jenkins → Manage Users → Create User**

```
Username:   alice
Password:   (set a strong password)
Full name:  Alice Johnson
Email:      alice@company.com
```

### Step 3 — Assign roles to users

**Manage Jenkins → Manage and Assign Roles → Assign Roles**

```
alice  → developer-role  (can trigger builds)
bob    → developer-role  + hotfix-only project role (can only trigger hotfix-* jobs)
carol  → readonly-role   (can view only)
```

### Step 4 — Each user generates a Jenkins API token

> API tokens are more secure than passwords and avoid CSRF issues.

1. User logs into Jenkins as themselves
2. Click their name (top right) → **Configure**
3. **API Token** → **Add new Token** → name it `buildbot` → **Generate**
4. Copy the token — Jenkins won't show it again

### Step 5 — Log into BuildBot

Open **http://localhost:5000**:
```
Jenkins Username:              alice
Jenkins Password or API Token: <paste token>
```

BuildBot:
- Verifies credentials against Jenkins `/api/json`
- Fetches the job list visible to this user
- Stores permissions in the session
- Uses Alice's own token when she triggers a build

### What happens when permissions are enforced

| Scenario | Result |
|---|---|
| Alice triggers `hotfix-build` (she has access) | ✅ Build starts |
| Alice tries `release-build` (no access) | ❌ "Your Jenkins account does not have Build permission for this job." |
| Carol (readonly) tries any build | ⛔ Blocked in BuildBot before reaching Jenkins |
| Alice's token expired / revoked | ❌ Login fails: "Jenkins authentication failed." |

---

## Local mode (users.json)

Used when `AUTH_MODE=local`, or for emergency admin accounts in Jenkins mode.

### Adding a user via admin panel

1. Log into BuildBot as `admin`
2. Click **👥 Users** in the header
3. Click **+ Add User**
4. Fill in:
   - **Username** — lowercase, letters/digits/underscores only
   - **Display Name** — shown in the header badge
   - **Role** — `admin` / `developer` / `readonly`
   - **Allowed Jobs** — comma-separated (shown when role = developer)
   - **Password** — live policy check confirms all 5 rules pass
5. Click **Add User**

### Adding a user via CLI

```powershell
cd "d:\ai module\NEW-ai-bot-atmpt2"
python scripts\manage_users.py add --username bob --display "Bob Smith" --role developer --jobs hotfix-build,release-build
```

Password is prompted interactively with policy validation.

### Password policy (fusion policy)

All passwords must meet **all five** rules:

| Rule | Example |
|---|---|
| Min 8 characters | `Sand@2026` |
| Uppercase letter (A–Z) | `S` |
| Lowercase letter (a–z) | `and` |
| Digit (0–9) | `2026` |
| Special character `!@#$%^&*-_+=?.,:;` | `@` |

### Managing local users via CLI

```powershell
# List all users
python scripts\manage_users.py list

# Change password (prompts with policy check)
python scripts\manage_users.py passwd --username bob

# Change role
python scripts\manage_users.py role --username bob --role admin

# Change allowed jobs
python scripts\manage_users.py jobs --username alice --jobs hotfix-build,release-build

# Disable (blocks login, keeps history)
python scripts\manage_users.py disable --username carol

# Re-enable
python scripts\manage_users.py enable --username carol

# Delete permanently
python scripts\manage_users.py remove --username bob
```

### Local roles and what they mean

| Role | Can trigger builds | Allowed jobs |
|---|---|---|
| `admin` | Any job | `*` (all) |
| `developer` | Listed jobs only | `["hotfix-build", "release-build"]` |
| `readonly` | None | `[]` |

---

## Admin panel — http://localhost:5000/admin/users

Accessible from the **👥 Users** link in the BuildBot header (admin role only).

### User table columns

| Column | Description |
|---|---|
| Username | Login name (monospace) |
| Display Name | Shown in chat header badge |
| Role | Colour-coded badge: admin (pink), developer (indigo), readonly (green) |
| Allowed Jobs | For local accounts; Jenkins users show jobs from session |
| Status | Active (green) or Disabled (grey) |
| Actions | ✏️ Edit · 🔑 Change password · ⏸ Disable/Enable · 🗑️ Delete |

### Action details

**Edit user** — change display name, role, allowed jobs. Jobs field hidden for admin/readonly roles.

**Change password** — live checklist shows each policy rule pass/fail as you type. Submit button stays disabled until all 5 pass and passwords match.

**Disable user** — blocks login immediately. User data is preserved. Useful for temporary suspension or off-boarding.

**Delete user** — permanent. The last active admin cannot be deleted (safety guard).

---

## Default accounts

Set during initial setup. **Change all passwords immediately after setup.**

| Username | Password | Role | Notes |
|---|---|---|---|
| `admin` | *(set on first run)* | admin | Emergency fallback admin — set a strong password |
| `alice` | *(set on first run)* | developer | Example — remove or repurpose |
| `bob` | *(set on first run)* | developer | Example — remove or repurpose |
| `carol` | *(set on first run)* | readonly | Example — remove or repurpose |

Create accounts using the admin panel or CLI:
```powershell
python scripts\manage_users.py add --username admin --role admin
```

Change a password:
```powershell
python scripts\manage_users.py passwd --username admin
```

---

## Security notes

- Passwords are hashed with `werkzeug` (scrypt) — never stored in plain text
- Jenkins API tokens are stored in the Flask session (encrypted with `FLASK_SECRET_KEY`)
- Sessions expire after 8 hours
- The last active admin is protected from deletion and disabling
- `config/users.json` is git-ignored (like `.env`)
- In Jenkins mode, BuildBot never stores Jenkins passwords long-term — only the session token

---

*Exterro · DevOps AI Challenge · September 2026*
