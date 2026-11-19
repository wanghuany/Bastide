"""A small local Python report client; stdout contains only aggregate JSON."""

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        default="monthly",
        choices=(
            "summary",
            "monthly",
            "annual",
            "category_ranking",
            "merchant_ranking",
            "year_over_year",
        ),
    )
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--category-id")
    args = parser.parse_args()
    base = os.environ.get("BASTIDE_PERSONAL_URL", "http://127.0.0.1:8899").rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        parser.error("Only a loopback HTTP API URL is permitted.")
    token = os.environ.get("BASTIDE_PERSONAL_TOKEN")
    if not token:
        parser.error("Set BASTIDE_PERSONAL_TOKEN using the local app's generated MCP token.")
    params = {"report": args.report, "start": args.start, "end": args.end}
    if args.category_id:
        params["category_id"] = args.category_id
    request = Request(
        base + "/api/agent/analysis?" + urlencode(params),
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=60) as response:
            print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
    except HTTPError:
        parser.exit(1, "Local report API rejected the request. Check the unlock state and token.\n")
    except URLError:
        parser.exit(1, "Start and unlock the local app before running an analysis.\n")


if __name__ == "__main__":
    main()
