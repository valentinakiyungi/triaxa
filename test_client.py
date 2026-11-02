"""Command-line client for Triaxa.

Examples:
    python test_client.py                              # run the built-in sample tickets through the queue
    python test_client.py analyze "I was charged twice"  # instant analysis
    python test_client.py submit "Login page is broken" --wait
    python test_client.py batch tickets.txt --wait      # one ticket per line
    python test_client.py status <job_id>
    python test_client.py list --status failed
    python test_client.py health
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

SAMPLES = [
    "Hey, I need help upgrading my subscription plan from Free to Pro.",
    "Our production dashboard has been down for 20 minutes and every API call returns 500. Customers are complaining!",
    "I can't log in — it says my account is locked after I reset my password.",
    "Just wanted to say the new export feature is great, thanks team.",
    "Could someone send me a quote for 50 enterprise seats?",
]


class Client:
    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: float = 180):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-API-Key"] = api_key
        self.timeout = timeout

    def _req(self, method: str, path: str, **kw) -> Any:
        resp = self.session.request(method, self.base_url + path, timeout=self.timeout, **kw)
        if resp.status_code == 204:
            return None
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text}
        if not resp.ok:
            raise SystemExit(f"Error {resp.status_code}: {body.get('detail', body)}")
        return body

    def health(self) -> Dict[str, Any]:
        resp = self.session.get(self.base_url + "/health/ready", timeout=15)
        return resp.json()

    def analyze(self, text: str) -> Dict[str, Any]:
        return self._req("POST", "/analyze", json={"text": text})

    def submit(self, text: str) -> Dict[str, Any]:
        return self._req("POST", "/jobs", json={"text": text})

    def batch(self, texts: List[str]) -> List[Dict[str, Any]]:
        return self._req("POST", "/jobs/batch", json={"items": [{"text": t} for t in texts]})

    def job(self, job_id: str) -> Dict[str, Any]:
        return self._req("GET", f"/jobs/{job_id}")

    def list(self, **params) -> Dict[str, Any]:
        return self._req("GET", "/jobs", params={k: v for k, v in params.items() if v})

    def wait(self, job_ids: List[str], poll: float = 1.0, timeout: float = 600) -> List[Dict[str, Any]]:
        deadline, pending, done = time.time() + timeout, list(job_ids), {}
        while pending and time.time() < deadline:
            for jid in list(pending):
                job = self.job(jid)
                if job["status"] in ("completed", "failed"):
                    done[jid] = job
                    pending.remove(jid)
                    print_job(job)
            if pending:
                time.sleep(poll)
        if pending:
            print(f"Timed out waiting for {len(pending)} job(s).", file=sys.stderr)
        return [done[j] for j in job_ids if j in done]


def print_job(job: Dict[str, Any]) -> None:
    text = job["text"] if len(job["text"]) <= 70 else job["text"][:67] + "..."
    print(f"\n[{job['status'].upper()}] {job['id']}  \"{text}\"")
    if job.get("result"):
        r = job["result"]
        print(f"  {r['category']} · {r['urgency']} · {r['sentiment']}  ({job.get('duration_ms')} ms)")
        print(f"  Summary:   {r['summary']}")
        print(f"  Next step: {r['action_item']}")
    if job.get("error"):
        print(f"  Error: {job['error']}")


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.getenv("TRIAXA_URL", "http://127.0.0.1:8000"))
    p.add_argument("--api-key", default=os.getenv("TRIAXA_API_KEY"))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("health")
    sub.add_parser("demo", help="queue the sample tickets and wait (default)")
    a = sub.add_parser("analyze"); a.add_argument("text")
    s = sub.add_parser("submit"); s.add_argument("text"); s.add_argument("--wait", action="store_true")
    b = sub.add_parser("batch"); b.add_argument("file", help="text file, one ticket per line ('-' for stdin)")
    b.add_argument("--wait", action="store_true")
    st = sub.add_parser("status"); st.add_argument("job_id")
    ls = sub.add_parser("list"); ls.add_argument("--status"); ls.add_argument("--category")
    ls.add_argument("--urgency"); ls.add_argument("--limit", type=int, default=20)
    args = p.parse_args(argv)
    c = Client(args.url, args.api_key)

    try:
        if args.cmd == "health":
            print(json.dumps(c.health(), indent=2))
        elif args.cmd == "analyze":
            print(json.dumps(c.analyze(args.text), indent=2))
        elif args.cmd == "submit":
            job = c.submit(args.text)
            print(f"Queued job {job['id']}")
            if args.wait:
                c.wait([job["id"]])
        elif args.cmd == "batch":
            fh = sys.stdin if args.file == "-" else open(args.file, encoding="utf-8")
            texts = [line.strip() for line in fh if line.strip()]
            jobs = c.batch(texts)
            print(f"Queued {len(jobs)} job(s)")
            if args.wait:
                c.wait([j["id"] for j in jobs])
            else:
                for j in jobs:
                    print(j["id"])
        elif args.cmd == "status":
            print_job(c.job(args.job_id))
        elif args.cmd == "list":
            data = c.list(status=args.status, category=args.category, urgency=args.urgency, limit=args.limit)
            for j in data["items"]:
                print_job(j)
            print(f"\n{len(data['items'])} of {data['total']} job(s)")
        else:
            health = c.health()
            llm = health.get("llm", {})
            print(f"Server: {args.url}  LLM: {llm.get('provider')} ({'ok' if llm.get('ok') else 'UNREACHABLE'})")
            if not llm.get("ok"):
                print(f"  {llm.get('error')}\n  Start LM Studio's server or set LLM_PROVIDER=mock to try without a model.")
            jobs = c.batch(SAMPLES)
            print(f"Queued {len(jobs)} sample tickets; waiting for results…")
            results = c.wait([j["id"] for j in jobs])
            ok = sum(1 for j in results if j["status"] == "completed")
            print(f"\nDone: {ok}/{len(SAMPLES)} completed.")
    except requests.ConnectionError:
        raise SystemExit(f"Cannot connect to Triaxa at {args.url}. Is the server running?")


if __name__ == "__main__":
    main()
