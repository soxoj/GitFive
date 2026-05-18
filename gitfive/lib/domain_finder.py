from googlesearch import search
import httpx

import json

from gitfive.lib.objects import GitfiveRunner
from gitfive.lib.utils import extract_domain


def guess_custom_domain(runner: GitfiveRunner):
    company = runner.target.company.lower()

    google = None
    hunter = None

    # Google
    try:
        if company != "google": # googlesearch doesn't return Google.com when searching "google"
            for url in search(company):
                if ("facebook" not in company and "facebook.com" in url) or ("twitter" not in company and "twitter.com" in url) :
                    continue
                google = extract_domain(url)
                break
    except Exception: # https://github.com/mxrch/GitFive/issues/15
        runner.rc.print("[!] Google Search failed, are you using a VPN/Proxy ?", style="italic")

    # Hunter.io — the public hunter.io/v2 endpoint now 303-redirects to
    # api.hunter.io and requires an API key (returns 401). Treat any non-200
    # or non-JSON response as "no result" instead of crashing the whole run.
    try:
        req = httpx.get(
            f"https://hunter.io/v2/domains-suggestion?query={company}",
            follow_redirects=True,
        )
        if req.status_code == 200:
            data = req.json()
            if results := data.get("data", [{}]):
                hunter = results[0].get("domain")
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        runner.rc.print("[!] Hunter.io lookup failed.", style="italic")

    if hunter and (not google or hunter in google):
        runner.rc.print(f'🔍 [Hunter.io] Found possible domain "{hunter}" for company "{company}"', style="light_green")
        return {hunter}
    elif hunter and google:
        runner.rc.print(f'🔍 [Hunter.io] Found possible domain "{hunter}" for company "{company}"', style="light_green")
        runner.rc.print(f'🔍 [Google] Found possible domain "{google}" for company "{company}"', style="light_green")
        return {hunter, google}
    elif google:
        runner.rc.print(f'🔍 [Google] Found possible domain "{google}" for company "{company}"', style="light_green")
        return {google}
    return set()