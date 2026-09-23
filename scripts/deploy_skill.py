#!/usr/bin/env python3
"""
deploy_skill.py -- create or update the Alexa skill via the ASK Skill
Management API (SMAPI), driven from Terraform via a null_resource +
local-exec (see terraform/skill.tf).

Why this exists: there is no native Terraform resource for an Alexa skill.
The AWS provider has no aws_alexa_* resources, and the only IaC path is
either the CloudFormation resource type Alexa::ASK::Skill or driving SMAPI
directly. This script does the latter, in Python.

SMAPI mechanics implemented here (verified against Amazon's own SMAPI docs,
not assumed):
  1. Exchange a long-lived LWA refresh token for a short-lived access token
     via POST https://api.amazon.com/auth/o2/token.
  2. Zip the local skill_package/ directory.
  3. Upload the zip to a presigned S3 URL obtained from
     POST /v1/skills/uploads.
  4. Create a new skill (POST /v1/skills/imports) or update an existing one
     (POST /v1/skills/{skillId}/imports), pointing at the uploaded package.
  5. Poll GET /v1/skills/imports/{importId} until the import succeeds or
     fails.

Credentials required (all via environment variables, never hardcoded):
  ASK_LWA_CLIENT_ID       -- from the LWA security profile used for SMAPI
  ASK_LWA_CLIENT_SECRET   -- from the same security profile
  ASK_LWA_REFRESH_TOKEN   -- obtained once via `ask util generate-lwa-tokens`
  ASK_VENDOR_ID           -- from developer.amazon.com/settings/console/mycid

These are deliberately NOT stored in Terraform variables or state -- they
are secrets and belong in environment variables / a secrets manager / CI
secrets, not in .tf files or tfstate.
"""

import argparse
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
SMAPI_BASE_URL = "https://api.amazonalexa.com"
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 300


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def _print_lwa_error_hint(raw_error_body: str) -> None:
    """
    LWA's token endpoint error codes map to a small, known set of causes.
    Printing a concrete hint here (instead of leaving the reader to guess
    from the raw JSON alone) is what makes this failure fixable without
    another round trip -- see README.md for the full explanation this hint is a short version of.
    """
    try:
        parsed = json.loads(raw_error_body)
    except (json.JSONDecodeError, TypeError):
        return

    error = parsed.get("error")
    if error == "unauthorized_client":
        print(
            "\nHINT: 'unauthorized_client' from LWA means the refresh token "
            "does not belong to the client_id/client_secret pair you sent "
            "with it. This happens when:\n"
            "  1. The client secret was regenerated on the LWA console "
            "AFTER the refresh token was generated -- regenerating the "
            "secret immediately invalidates every refresh token issued "
            "under the old secret.\n"
            "  2. ASK_LWA_CLIENT_ID/ASK_LWA_CLIENT_SECRET are from a "
            "different LWA security profile than the one used when you "
            "ran `ask util generate-lwa-tokens` to get "
            "ASK_LWA_REFRESH_TOKEN.\n"
            "  3. One of the three env vars is stale (left over from a "
            "previous attempt) and doesn't match the other two.\n"
            "Fix: regenerate the refresh token against the CURRENT client "
            "ID/secret shown on the LWA console for this security profile "
            "(`ask util generate-lwa-tokens`), then re-export all three "
            "ASK_LWA_* variables together from that single run -- don't "
            "mix values from different runs or different security "
            "profiles. See README.md.",
            file=sys.stderr,
        )
    elif error == "invalid_client":
        print(
            "\nHINT: 'invalid_client' from LWA means ASK_LWA_CLIENT_ID or "
            "ASK_LWA_CLIENT_SECRET itself is wrong (not the refresh token) "
            "-- double check both were copied exactly from the LWA "
            "console's Web Settings for this security profile, with no "
            "extra whitespace or truncation. See README.md.",
            file=sys.stderr,
        )
    elif error == "invalid_grant":
        print(
            "\nHINT: 'invalid_grant' from LWA means ASK_LWA_REFRESH_TOKEN "
            "itself is invalid, expired, or was already exchanged/revoked. "
            "Refresh tokens from `ask util generate-lwa-tokens` don't "
            "expire under normal use, but are invalidated if the security "
            "profile's client secret is regenerated, or if you're on an "
            "old copy from a previous, unrelated attempt. Re-run `ask "
            "util generate-lwa-tokens` to get a fresh one. See "
            "README.md.",
            file=sys.stderr,
        )


def get_access_token() -> str:
    """Exchange the LWA refresh token for a short-lived access token."""
    client_id = _require_env("ASK_LWA_CLIENT_ID")
    client_secret = _require_env("ASK_LWA_CLIENT_SECRET")
    refresh_token = _require_env("ASK_LWA_REFRESH_TOKEN")

    # IMPORTANT: these values are URL-encoded via urlencode(), not
    # dropped into the body with an f-string. LWA refresh tokens and
    # client secrets routinely contain characters (+, /, =, &) that are
    # not safe to place unescaped into an application/x-www-form-urlencoded
    # body -- an unescaped "&" or "=" in a credential silently truncates
    # or corrupts the field boundaries. LWA's token endpoint returns a
    # bare 400 Bad Request for a malformed body like that, which is what
    # was happening here before this fix.
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")

    req = Request(
        LWA_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        # LWA's token endpoint always returns a JSON body describing why
        # the request was rejected (e.g. {"error": "invalid_grant",
        # "error_description": "..."}) -- without catching this, the
        # caller only ever sees a bare "HTTP Error 400: Bad Request" with
        # no indication of which credential is wrong or why. Surfacing
        # the real body here is what makes this failure diagnosable at
        # all, the same way _smapi_request() already does for SMAPI
        # calls further down the pipeline.
        raw = e.read().decode("utf-8")
        print(f"LWA token exchange failed: HTTP {e.code}\n{raw}", file=sys.stderr)
        _print_lwa_error_hint(raw)
        sys.exit(1)
    return payload["access_token"]


def _smapi_request(method: str, path: str, access_token: str, body: dict = None):
    url = f"{SMAPI_BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req) as resp:
            location = resp.headers.get("Location")
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return resp.status, parsed, location
    except HTTPError as e:
        raw = e.read().decode("utf-8")
        print(f"SMAPI request failed: {method} {path} -> HTTP {e.code}\n{raw}", file=sys.stderr)
        sys.exit(1)


def zip_skill_package(skill_package_dir: Path, output_zip: Path) -> Path:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in skill_package_dir.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(skill_package_dir)
                zf.write(file_path, arcname)

    return output_zip


def create_upload_url(access_token: str) -> dict:
    status, body, _ = _smapi_request("POST", "/v1/skills/uploads", access_token)
    if status != 201:
        print(f"Unexpected status creating upload URL: {status}", file=sys.stderr)
        sys.exit(1)
    return body


def upload_package(upload_url: str, zip_path: Path) -> None:
    data = zip_path.read_bytes()
    req = Request(upload_url, data=data, method="PUT")
    with urlopen(req) as resp:
        if resp.status not in (200, 201):
            print(f"Unexpected status uploading package: {resp.status}", file=sys.stderr)
            sys.exit(1)


def create_skill(access_token: str, vendor_id: str, upload_location: str) -> str:
    """Create a new skill and import the uploaded package. Returns importId."""
    status, _, location = _smapi_request(
        "POST",
        "/v1/skills/imports",
        access_token,
        body={"vendorId": vendor_id, "location": upload_location},
    )
    if status != 202 or not location:
        print(f"Unexpected response creating skill: status={status}, location={location}", file=sys.stderr)
        sys.exit(1)
    # location looks like /v1/skills/imports/{importId}
    return location.rsplit("/", 1)[-1]


def update_skill(access_token: str, skill_id: str, upload_location: str) -> str:
    """Import an updated package into an existing skill. Returns importId."""
    status, _, location = _smapi_request(
        "POST",
        f"/v1/skills/{skill_id}/imports",
        access_token,
        body={"location": upload_location},
    )
    if status != 202 or not location:
        print(f"Unexpected response updating skill: status={status}, location={location}", file=sys.stderr)
        sys.exit(1)
    return location.rsplit("/", 1)[-1]


def poll_import_status(access_token: str, import_id: str) -> dict:
    """
    Poll until the import succeeds or fails. Returns the raw response body
    from the final successful status check.

    NOTE on skillId retrieval: despite Amazon's documented response schema
    for GET /v1/skills/imports/{importId} listing only `status` and
    `skill: {expiresAt, location, eTag}` (no `skillId` field), a real
    `skillId` has been observed in practice in the `skill` object on both
    success AND failure responses for brand-new skill creation. This
    function reads it opportunistically when present (see the FAILED
    branch below), but does not rely on it being documented/guaranteed --
    `find_skill_id`'s List-Skills fallback in main() remains the
    authoritative path for the success case, since the docs still don't
    promise this field will always be there.
    """
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        status, body, _ = _smapi_request(
            "GET", f"/v1/skills/imports/{import_id}", access_token
        )
        import_status = body.get("status")
        print(f"Import status: {import_status}")
        if import_status == "SUCCEEDED":
            return body
        if import_status == "FAILED":
            print(f"Import failed: {json.dumps(body, indent=2)}", file=sys.stderr)
            _print_import_failure_hint(body)
            sys.exit(1)
        time.sleep(POLL_INTERVAL_SECONDS)

    print("Timed out waiting for skill import to complete.", file=sys.stderr)
    sys.exit(1)


def _print_import_failure_hint(import_status_body: dict) -> None:
    """
    IMPORTANT: on a brand-new skill creation (no --skill-id passed), SMAPI
    can assign a real skillId and create a partial skill record BEFORE
    validation fails on the manifest or interaction model -- and the
    automatic rollback of that partial skill can itself fail
    ("ROLLBACK_FAILED"), leaving an orphaned, broken skill in the
    developer account. If this function finds a skillId in a FAILED
    response, it prints it explicitly so the caller doesn't retry blindly
    and create a second, duplicate skill while the first one lingers
    unnoticed in the Alexa Developer Console.
    """
    skill_id = import_status_body.get("skill", {}).get("skillId")
    if not skill_id:
        return

    print(
        f"\nHINT: a skill record was created before this import failed -- "
        f"skillId: {skill_id}\n"
        "If any resource above shows \"ROLLBACK_FAILED\" (not just "
        "\"FAILED\"), Amazon's automatic cleanup of that partial skill "
        "also failed, so it is very likely still sitting in your Alexa "
        "Developer Console in a broken state.\n"
        "Before retrying:\n"
        "  1. Fix whatever validation error is listed above (e.g. an "
        "interaction model or manifest problem) in this project's "
        "skill_package/ files.\n"
        "  2. Check https://developer.amazon.com/alexa/console/ask for a "
        "skill matching this ID (or named \"Jarvis AI\") left "
        "over from this failed attempt. Either delete it there, or set "
        f"alexa_skill_id = \"{skill_id}\" in terraform.tfvars and re-apply "
        "-- with --skill-id set, this script takes the UPDATE path "
        "against the existing skill instead of creating a new one, which "
        "fixes it in place rather than leaving it orphaned.\n"
        "  3. Do NOT simply retry with alexa_skill_id still empty -- that "
        "creates ANOTHER new skill and leaves this one behind.",
        file=sys.stderr,
    )


def find_skill_id(access_token: str, vendor_id: str, skill_name: str) -> str:
    """
    Fallback used only for brand-new skill creation, where the import
    status response does not include a skillId (see poll_import_status's
    docstring for why). Lists skills for the vendor and returns the most
    recently updated one whose en-US name matches skill_name.

    This is a heuristic, not a guaranteed-correct lookup -- if this
    vendor account has multiple skills with the same name, or the name
    match is ambiguous, this will need manual verification. Printed
    clearly to stderr so it is never silently wrong.
    """
    status, body, _ = _smapi_request(
        "GET",
        f"/v1/skills?vendorId={vendor_id}&maxResults=50",
        access_token,
    )
    candidates = [
        s
        for s in body.get("skills", [])
        if s.get("nameByLocale", {}).get("en-US") == skill_name
    ]
    if not candidates:
        print(
            f"WARNING: could not find a skill named '{skill_name}' for vendor "
            f"{vendor_id} via List Skills. Check the Alexa Developer Console "
            "manually to retrieve the new skill ID.",
            file=sys.stderr,
        )
        return ""

    candidates.sort(key=lambda s: s.get("lastUpdated", ""), reverse=True)
    if len(candidates) > 1:
        print(
            f"WARNING: found {len(candidates)} skills named '{skill_name}' for "
            "this vendor. Using the most recently updated one, but verify this "
            "is correct in the Alexa Developer Console.",
            file=sys.stderr,
        )
    return candidates[0]["skillId"]


def inject_lambda_arn(skill_package_dir: Path, lambda_arn: str) -> None:
    """Rewrite skill.json's endpoint URI with the real Lambda ARN before zipping."""
    skill_json_path = skill_package_dir / "skill.json"
    manifest = json.loads(skill_json_path.read_text())
    manifest["manifest"]["apis"]["custom"]["endpoint"]["uri"] = lambda_arn
    skill_json_path.write_text(json.dumps(manifest, indent=2))


def inject_icon_uris(skill_package_dir: Path, small_icon_uri: str, large_icon_uri: str) -> None:
    """
    Rewrite skill.json's smallIconUri/largeIconUri with the real, public
    S3 URLs before zipping -- same pattern as inject_lambda_arn above.
    Both must be publicly reachable HTTPS URLs (confirmed against
    Amazon's "Define Skill Store Details for Publication" docs) --
    terraform/icons.tf provisions the public-read S3 bucket these URIs
    point at.

    No-ops (leaves skill.json's existing values untouched) if either URI
    is empty, so this script still works standalone/in tests without
    requiring icon URLs to be passed.
    """
    if not small_icon_uri and not large_icon_uri:
        return

    skill_json_path = skill_package_dir / "skill.json"
    manifest = json.loads(skill_json_path.read_text())
    for locale_data in manifest["manifest"]["publishingInformation"]["locales"].values():
        if small_icon_uri:
            locale_data["smallIconUri"] = small_icon_uri
        if large_icon_uri:
            locale_data["largeIconUri"] = large_icon_uri
    skill_json_path.write_text(json.dumps(manifest, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-package-dir", required=True, help="Path to the skill_package directory")
    parser.add_argument("--lambda-arn", required=True, help="ARN of the deployed Lambda function")
    parser.add_argument("--skill-id", default="", help="Existing skill ID to update. If empty, a new skill is created.")
    parser.add_argument("--build-dir", default="/tmp/skill_package_build", help="Scratch directory for the zip")
    parser.add_argument(
        "--small-icon-uri",
        default="",
        help="Public HTTPS URL of the 108x108 skill icon (terraform output skill_icon_small_uri). Optional -- leaves skill.json's existing value untouched if omitted.",
    )
    parser.add_argument(
        "--large-icon-uri",
        default="",
        help="Public HTTPS URL of the 512x512 skill icon (terraform output skill_icon_large_uri). Optional -- leaves skill.json's existing value untouched if omitted.",
    )
    args = parser.parse_args()

    skill_package_dir = Path(args.skill_package_dir).resolve()
    inject_lambda_arn(skill_package_dir, args.lambda_arn)
    inject_icon_uris(skill_package_dir, args.small_icon_uri, args.large_icon_uri)

    access_token = get_access_token()

    zip_path = zip_skill_package(skill_package_dir, Path(args.build_dir) / "skill_package.zip")

    upload_info = create_upload_url(access_token)
    upload_package(upload_info["uploadUrl"], zip_path)

    manifest = json.loads((skill_package_dir / "skill.json").read_text())
    skill_name = manifest["manifest"]["publishingInformation"]["locales"]["en-US"]["name"]

    if args.skill_id:
        import_id = update_skill(access_token, args.skill_id, upload_info["uploadUrl"])
        # Updating an existing skill: the skill ID is already known, we
        # only need to poll to confirm the import itself succeeded.
        poll_import_status(access_token, import_id)
        skill_id = args.skill_id
    else:
        vendor_id = _require_env("ASK_VENDOR_ID")
        import_id = create_skill(access_token, vendor_id, upload_info["uploadUrl"])
        poll_import_status(access_token, import_id)
        # See find_skill_id's docstring: the import status response has no
        # documented skillId field for new-skill creation, so this falls
        # back to a List Skills lookup by name.
        skill_id = find_skill_id(access_token, vendor_id, skill_name)

    print(json.dumps({"skillId": skill_id}))


if __name__ == "__main__":
    main()
