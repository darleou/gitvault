#!/usr/bin/env python3
"""GitVault — backup all public repos of a GitHub user into Releases of this repo.

Each source repo gets its own Release (tag = repo name). Every backup uploads
a full `git clone --mirror` archive named `<repo>_<YYYYMMDD>.zip` as a release
asset. Only repos whose `pushed_at` changed since the last run are backed up.

Env vars:
  BACKUP_USERNAME    GitHub user whose public repos are backed up
  GITHUB_TOKEN       token with contents:write on this repo
  GITHUB_REPOSITORY  owner/name of this vault repo (set by Actions)

Flags: --dry-run (detect + zip, no upload/state change), --force-all
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
KEEP_BACKUPS = 5
MAX_ASSET_BYTES = 2 * 1024**3  # GitHub release asset hard limit


def gh_request(method, url, token, data=None, content_type=None, ok404=False):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    body = None
    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            body = bytes(data)
            req.add_header("Content-Type", content_type or "application/octet-stream")
        else:
            body = json.dumps(data).encode()
            req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, body) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404 and ok404:
            return None
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def paginate(url, token):
    page = 1
    while True:
        sep = "&" if "?" in url else "?"
        items = gh_request("GET", f"{url}{sep}per_page=100&page={page}", token)
        if not items:
            return
        yield from items
        if len(items) < 100:
            return
        page += 1


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def mirror_and_zip(clone_url, repo_name, workdir):
    mirror_dir = os.path.join(workdir, f"{repo_name}.git")
    subprocess.run(
        ["git", "clone", "--mirror", "--quiet", clone_url, mirror_dir],
        check=True, capture_output=True, text=True,
    )
    refs = subprocess.run(
        ["git", "-C", mirror_dir, "for-each-ref", "--count=1"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not refs:
        return None  # empty repo, nothing to back up
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    base = os.path.join(workdir, f"{repo_name}_{date}")
    return shutil.make_archive(base, "zip", root_dir=workdir, base_dir=f"{repo_name}.git")


def ensure_release(vault, repo, token):
    rel = gh_request("GET", f"{API}/repos/{vault}/releases/tags/{repo['name']}", token, ok404=True)
    if rel:
        return rel
    return gh_request("POST", f"{API}/repos/{vault}/releases", token, data={
        "tag_name": repo["name"],
        "name": repo["name"],
        "body": f"Automated mirror backups of {repo['html_url']}\n\n"
                f"Restore: unzip, then `git clone <extracted-dir> {repo['name']}`",
    })


def upload_asset(vault, release, zip_path, token):
    name = os.path.basename(zip_path)
    # replace a same-name asset (second backup on the same day)
    for asset in gh_request("GET", f"{API}/repos/{vault}/releases/{release['id']}/assets?per_page=100", token) or []:
        if asset["name"] == name:
            gh_request("DELETE", f"{API}/repos/{vault}/releases/assets/{asset['id']}", token)
    upload_url = release["upload_url"].split("{")[0]
    with open(zip_path, "rb") as f:
        payload = f.read()
    gh_request("POST", f"{upload_url}?name={urllib.parse.quote(name)}", token,
               data=payload, content_type="application/zip")


def prune_assets(vault, release, token):
    assets = gh_request("GET", f"{API}/repos/{vault}/releases/{release['id']}/assets?per_page=100", token) or []
    assets.sort(key=lambda a: a["created_at"], reverse=True)
    for old in assets[KEEP_BACKUPS:]:
        print(f"    pruning old backup {old['name']}")
        gh_request("DELETE", f"{API}/repos/{vault}/releases/assets/{old['id']}", token)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-all", action="store_true")
    args = parser.parse_args()

    username = os.environ.get("BACKUP_USERNAME")
    token = os.environ.get("GITHUB_TOKEN")
    vault = os.environ.get("GITHUB_REPOSITORY")
    if not username or not token or (not vault and not args.dry_run):
        sys.exit("BACKUP_USERNAME, GITHUB_TOKEN and GITHUB_REPOSITORY must be set")

    state = load_state()
    repos = [r for r in paginate(f"{API}/users/{username}/repos?type=owner", token) if not r["private"]]
    print(f"{len(repos)} public repos found for {username}")

    backed_up, skipped, failed = [], [], []
    for repo in repos:
        name = repo["name"]
        if not args.force_all and state.get(name, {}).get("pushed_at") == repo["pushed_at"]:
            skipped.append(name)
            continue
        print(f"==> backing up {name} (pushed_at {repo['pushed_at']})")
        try:
            with tempfile.TemporaryDirectory() as workdir:
                zip_path = mirror_and_zip(repo["clone_url"], name, workdir)
                if zip_path is None:
                    print("    empty repo, skipped")
                    skipped.append(f"{name} (empty)")
                    continue
                size = os.path.getsize(zip_path)
                print(f"    {os.path.basename(zip_path)} ({size / 1024:.0f} KB)")
                if size > MAX_ASSET_BYTES:
                    raise RuntimeError(f"zip is {size} bytes, exceeds the 2 GB release asset limit")
                if not args.dry_run:
                    release = ensure_release(vault, repo, token)
                    upload_asset(vault, release, zip_path, token)
                    prune_assets(vault, release, token)
            if not args.dry_run:
                state[name] = {
                    "pushed_at": repo["pushed_at"],
                    "last_backup": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                save_state(state)  # save incrementally so a late failure loses nothing
            backed_up.append(name)
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr)
            failed.append(f"{name}: {e}")

    summary = (
        f"### GitVault run — {username}\n\n"
        f"- checked: {len(repos)} repos\n"
        f"- backed up: {len(backed_up)} {backed_up}\n"
        f"- unchanged/skipped: {len(skipped)}\n"
        f"- failed: {len(failed)}\n"
        + "".join(f"  - {f}\n" for f in failed)
    )
    print("\n" + summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
