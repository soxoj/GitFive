import json

import trio
from bs4 import BeautifulSoup
from alive_progress import alive_bar


from gitfive.lib.utils import *
from gitfive.lib import github
from gitfive.lib.instruments import TrioAliveProgress


def _extract_payload(raw_body: str):
    """
    The new GitHub commits page renders an embedded JSON payload inside a
    `<script type="application/json">` tag. The old `<li class="js-commits-list-item">`
    DOM has been removed. The payload contains a `commitGroups` list whose entries
    each hold a `commits` list with `oid` / `authors` / `bodyMessageHtml` fields —
    everything Metamon needs to map fake commit hashes back to the recognised
    GitHub user (the `authors[1]` entry, if any).
    """
    body = BeautifulSoup(raw_body, 'html.parser')
    for s in body.find_all('script', {'type': 'application/json'}):
        text = s.string or ''
        if 'commitGroups' in text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            payload = data.get('payload')
            if isinstance(payload, dict) and 'commitGroups' in payload:
                return payload
    return None


def _iter_commits(payload):
    for group in payload.get('commitGroups', []) or []:
        for commit in group.get('commits', []) or []:
            yield commit


def _target_author(commit):
    """
    Each Metamon commit has two `authors` entries: the local committer
    (`gitfive_hunter`, login=None) and the impersonated co-author. We want the
    one that GitHub successfully linked to a real account — i.e. has a non-null
    `login`. Returns None when nothing was matched (the email is unknown to GH).
    """
    for author in commit.get('authors', []) or []:
        login = author.get('login')
        if login and login != 'gitfive_hunter':
            return author
    return None


async def fetch_avatar(runner: GitfiveRunner, email: str, avatar_link: str, username: str,
                        out: Dict[str, str|bool], check_only: bool):
    async with runner.limiters["commits_fetch_avatar"]:
        is_target = (username.lower() == runner.target.username.lower())
        if check_only:
            if is_target:
                runner.rc.print(f"[+] [Target's email] 🐱 {email} -> @{username}", style="cyan")

            out[email] = {
                "avatar": avatar_link,
                "username": username,
                "is_target": is_target
            }
        else:
            full_name = await github.fetch_profile_name(runner, username)
            _name_str = ""
            if full_name:
                _name_str = f" [{full_name}]"

            if is_target:
                runner.rc.print(f"[+] [TARGET FOUND] 🐱 {email} -> @{username}{_name_str}", style="green bold")
            else:
                runner.rc.print(f"[+] 🐱 {email} -> @{username}{_name_str}")

            out[email] = {
                "avatar": avatar_link,
                "full_name": full_name,
                "username": username,
                "is_target": is_target
            }

async def fetch_commits(runner: GitfiveRunner, repo_name: str, emails_index: Dict[str, str],
                        last_hash: str, page: int, out: Dict[str, str|bool], check_only: bool):
    async with runner.limiters["commits_scrape"]:
        if page == 0:
            req = await runner.as_client.get(f"https://github.com/{runner.creds.username}/{repo_name}/commits/mirage")
        else:
            req = await runner.as_client.get(f"https://github.com/{runner.creds.username}/{repo_name}/commits/mirage?after={last_hash}+{page}&branch=mirage")

        if req.status_code == 429:
            exit(f'Rate-limit detected, please adjust the CapacityLimiter.\nCurrent CapacityLimiter : {runner.limiters["commits_scrape"]}')

        payload = _extract_payload(req.text)
        if payload is None:
            return

        async with trio.open_nursery() as nursery:
            for commit in _iter_commits(payload):
                hexsha = commit.get('oid')
                if not hexsha or hexsha not in emails_index:
                    continue
                target = _target_author(commit)
                if target is None:
                    continue

                email = emails_index[hexsha]
                avatar_link = target.get('avatarUrl')
                username = target.get('login')

                nursery.start_soon(fetch_avatar, runner, email, avatar_link, username, out, check_only)

async def scrape(runner: GitfiveRunner, repo_name: str, emails_index: Dict[str, str], check_only=False):
    out = {}

    req = await runner.as_client.get(f"https://github.com/{runner.creds.username}/{repo_name}/commits/mirage")
    if req.status_code != 200:
        exit(f"Couldn't fetch the commits page (HTTP {req.status_code}).")

    payload = _extract_payload(req.text)
    if payload is None:
        body = BeautifulSoup(req.text, 'html.parser')
        if is_repo_empty(body):
            exit("Empty repository.")
        exit("Couldn't parse the commits page payload.")

    last_hash = (payload.get('currentCommit') or {}).get('oid') \
        or (payload.get('refInfo') or {}).get('currentOid')
    if not last_hash:
        exit("Couldn't fetch the last hash.")

    _, total = await get_commits_count(runner, raw_body=req.text)
    if not total:
        # Fall back to counting whatever the payload already gave us.
        total = sum(len(g.get('commits', []) or []) for g in payload.get('commitGroups', []) or [])
    if not total:
        return out

    to_request = [0] + list(range(-1, total-1, 35))[1:]

    with alive_bar(total, receipt=False, enrich_print=False, title="Fetching committers...") as bar:
        instrument = TrioAliveProgress(fetch_commits, 35, bar)

        trio.lowlevel.add_instrument(instrument)

        async with trio.open_nursery() as nursery:
            for page in to_request:
                nursery.start_soon(fetch_commits, runner, repo_name, emails_index, last_hash, page, out, check_only)

        trio.lowlevel.remove_instrument(instrument)

    return out
