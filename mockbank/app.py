"""MockBank: a small synthetic legacy banking application (docs/spec.md section 4).

Legacy characteristics on purpose: a shell page whose content lives in an iframe
(the top-level URL never changes), table-based layout, server-rendered pages,
no test IDs, and weak semantics (the Member ID field has no <label>; its label is
only the adjacent table cell).

Flow:
  GET  /            shell page; <iframe name="main" src="/members">
  GET  /members     Member ID input + Search (form POSTs to /member)
  POST /member      Member Detail table (Member ID | <id>, Savings | <balance>)
                    or "No member found"; if a one-shot fault is armed it is
                    consumed here and the POST is redirected (307, body kept) to
                    the intermediate page
  POST /notice      "System notice" page, Continue re-POSTs /member
  POST /override    "Supervisor override required" page, Acknowledge re-POSTs /member
  GET/POST /settings  out-of-band fault-control page (never automation-allowed)

Member lookup is POST only; the member ID never appears in a URL.

Run locally:  python -m mockbank.app [port]   (default http://localhost:8000/)
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from mockbank import faults

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Synthetic data only. 12345 is the demo member with the known savings balance; 24680 is a
# second synthetic member so a human can navigate to a *different* member during HITL tests
# (spec 15.7 / 17.4 "wrong-member human navigation"); anything else is "No member found".
MEMBERS: dict[str, dict[str, str]] = {
    "12345": {"name": "Alex Sample", "status": "Active", "savings": "$1,234.56"},
    "24680": {"name": "Jordan Example", "status": "Active", "savings": "$88.20"},
}

app = FastAPI(title="MockBank", docs_url=None, redoc_url=None, openapi_url=None)
_templates = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)


def _render(template: str, status_code: int = 200, **context: object) -> HTMLResponse:
    return HTMLResponse(_templates.get_template(template).render(**context), status_code=status_code)


async def _form(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body with the stdlib only."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


@app.get("/")
async def shell() -> Response:
    return _render("index.html")


@app.get("/members")
async def members() -> Response:
    return _render("members.html")


@app.post("/member")
async def member(request: Request) -> Response:
    form = await _form(request)
    member_id = form.get("member_id", "").strip()

    # One-shot faults are applied (and consumed) here, before the lookup.
    # 307 keeps the POST method and body, so the member ID travels in the body, never the URL.
    fault = faults.consume_next()
    if fault == faults.INTERSTITIAL:
        return RedirectResponse("/notice", status_code=307)
    if fault == faults.UNKNOWN:
        return RedirectResponse("/override", status_code=307)

    record = MEMBERS.get(member_id)
    if record is None:
        return _render("notfound.html")
    return _render("member.html", member_id=member_id, member=record)


@app.post("/notice")
async def notice(request: Request) -> Response:
    form = await _form(request)
    return _render("notice.html", member_id=form.get("member_id", ""))


@app.post("/override")
async def override(request: Request) -> Response:
    form = await _form(request)
    return _render("override.html", member_id=form.get("member_id", ""))


@app.get("/settings")
async def settings() -> Response:
    return _render("settings.html", armed=faults.snapshot())


@app.post("/settings")
async def settings_update(request: Request) -> Response:
    form = await _form(request)
    try:
        if form.get("clear"):
            faults.reset()
        for name in faults.FAULTS:
            if name in form:
                faults.arm(name, form[name])
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return RedirectResponse("/settings", status_code=303)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run(app, host="localhost", port=port)
