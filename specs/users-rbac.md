# Users and RBAC

## Overview

The platform gains a user management system with role-based access control (RBAC). Users are created via a REST API and issued an API key on creation. Roles grant read and/or write access to specific buckets and are declared in `platform_config.yaml`. All data endpoints require authentication from day one.

## Goals

- All API callers are identified by a bearer token tied to a user identity.
- Access to bucket operations (read entities, write entities) is governed by roles.
- Roles and their bucket-level permissions are config-as-code — no runtime UI needed to manage them.
- Users can be created, listed, updated, and deleted via a REST API secured by an admin key.

## Non-Goals

- Authentication flows (OAuth, login pages, token refresh) — this spec covers API key issuance and authorization only.
- Fine-grained per-collection permissions — permissions are at the bucket level.
- API key expiry — keys are valid indefinitely until the user is deleted.
- Multi-bucket collections — collections remain 1-to-1 with buckets.

## Authentication

All endpoints require an `Authorization: Bearer <key>` header.

- **Admin endpoints** (`/users/*`) — validated against `STIX_HUB_ADMIN_API_KEY` (environment variable, never committed to version control).
- **Data endpoints** (TAXII routes, etc.) — validated against a user's API key stored in the database.

Requests without a valid key return `401 Unauthorized`. Requests with a valid key but insufficient bucket permission return `403 Forbidden`.

## API Key Issuance

On `POST /users`, the platform:

1. Generates a 32-byte cryptographically random key using `secrets.token_hex(32)` (64-character hex string).
2. Stores a SHA-256 hash of the key in the `users` table.
3. Returns the plaintext key once in the response — it is never retrievable again.

## Config Schema

Roles are declared in `platform_config.yaml` under a top-level `roles` key.

```yaml
roles:
  - name: analyst
    can_read:
      - raw-intel
    can_write: []

  - name: collector
    can_read: []
    can_write:
      - raw-intel

  - name: admin
    can_read:
      - raw-intel
    can_write:
      - raw-intel
```

Rules:
- `can_read` and `can_write` each list bucket names.
- A bucket name listed in a role must resolve to a known bucket; unknown bucket names are a fatal startup error.
- A role with empty `can_read` and empty `can_write` is valid but grants no access.
- A user can see a collection if they have read or write access to that collection's bucket.
- Effective permissions are the union of all permissions across all of the user's roles.

## User API

All `/users` endpoints require `Authorization: Bearer <STIX_HUB_ADMIN_API_KEY>`.

### Create user

```
POST /users
```

Request body:
```json
{
  "email": "alice@example.com",
  "roles": ["analyst"]
}
```

Response includes the generated API key (returned once only):
```json
{
  "id": "...",
  "email": "alice@example.com",
  "roles": ["analyst"],
  "created_at": "...",
  "api_key": "<plaintext key, shown once>"
}
```

- `email` must be unique.
- Each entry in `roles` must match a role declared in config.

### List users

```
GET /users
```

Returns an array of user objects (no `api_key` field).

### Update user roles

```
PATCH /users/{id}
```

Request body:
```json
{
  "roles": ["analyst", "collector"]
}
```

Replaces the user's role list. Returns the updated user object (no `api_key` field).

- Each entry in `roles` must match a role declared in config.

### Delete user

```
DELETE /users/{id}
```

Removes the user and invalidates their API key. Returns 204 on success, 404 if not found.

## User Model

| Field      | Type            | Notes                                        |
|------------|-----------------|----------------------------------------------|
| id         | UUID            | Platform-generated                           |
| email      | string          | Unique                                       |
| roles      | list of strings | Role names from config                       |
| api_key    | string          | SHA-256 hash of the issued key               |
| created_at | datetime        |                                              |

Users are persisted in the database. The plaintext key is never stored.

## Authorization Enforcement

When a request arrives at a data endpoint:

1. Extract the bearer token from `Authorization` header. Missing header → `401`.
2. Look up the user by SHA-256 hash of the token. Not found → `401`.
3. For the target bucket, check the user's effective permissions (union of all roles). Insufficient permission → `403`.

Collection visibility: a collection is included in `GET /taxii2/collections` only if the caller has read or write access to its bucket.

## Startup Validation

At startup, after loading `platform_config.yaml`:

1. Validate that every bucket name referenced in any role's `can_read` or `can_write` list corresponds to a declared bucket. Unknown bucket references are a fatal startup error.
2. Roles are registered in `app.state` for the lifetime of the process (no DB storage for roles).

## Domain Models

- `RoleConfig` — holds `name`, `can_read: list[str]`, `can_write: list[str]`.
- `UserModel` (ORM) — maps to a `users` table with `id`, `email`, `roles` (JSON array of role name strings), `api_key` (SHA-256 hash), `created_at`.
- `UserDomain` (Pydantic) — API-facing representation, no `api_key` field except on creation response.

## Error Cases

| Condition                          | HTTP Status              |
|------------------------------------|--------------------------|
| Missing or invalid bearer token    | 401 Unauthorized         |
| Email already exists               | 409 Conflict             |
| Unknown role in create/patch       | 422 Unprocessable Entity |
| User not found                     | 404 Not Found            |
| Insufficient bucket permission     | 403 Forbidden            |
| Role references unknown bucket     | startup crash            |
