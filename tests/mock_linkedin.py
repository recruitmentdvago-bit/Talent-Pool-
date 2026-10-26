"""A stand-in LinkedIn for tests: serves search results and profile pages in the
same reading order LinkedIn uses, sets a li_at cookie on login, and can raise a
checkpoint after N profile views. It exists so the browser flow can be tested
end to end without touching the real site.
"""
from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PEOPLE = []
_TITLES = ["Data Analyst", "Senior Data Analyst", "Data Engineer", "BI Developer",
           "MIS Analyst", "Data Scientist", "ETL Developer", "Analytics Manager"]
_LOCS = (["Karachi, Sindh, Pakistan"] * 9 + ["Clifton, Karachi, Pakistan"] * 3
         + ["Lahore, Punjab, Pakistan"] * 3 + ["Dubai, United Arab Emirates"] * 2)
for i in range(17):
    t = _TITLES[i % len(_TITLES)]
    PEOPLE.append({
        "slug": f"person-{i:02d}", "name": f"Person Number{i:02d}",
        "title": t, "company": ["Habib Bank Limited", "Easypaisa", "Systems Limited", "Jazz"][i % 4],
        "headline": f"{t} at {['Habib Bank Limited','Easypaisa','Systems Limited','Jazz'][i % 4]}",
        "location": _LOCS[i % len(_LOCS)],
        "truncated": i % 3 == 0,
    })


def profile_lines(p: dict) -> list[str]:
    L = [p["name"], "· 2nd", p["headline"], f"{p['location']} · Contact info", "500+ connections",
         "Message", "Connect", "More", "About",
         f"{p['title']} with five years of SQL, Power BI and Excel reporting for banking and fintech.",
         "…see more", "Experience",
         p["title"], f"{p['company']} · Full-time", "Mar 2021 - Present · 4 yrs 7 mos",
         p["location"] + " · On-site", "Built dashboards in Power BI and wrote complex SQL for MIS reporting.",
         "Data Analyst", "Faysal Bank · Full-time", "Jan 2019 - Feb 2021 · 2 yrs 2 mos", "Karachi, Pakistan",
         "Daily reporting in Excel and SQL."]
    if p["truncated"]:
        L.append("Show all 4 experiences")
    L += ["Education", "NED University of Engineering and Technology",
          "Bachelor of Science - BS, Computer Science", "2014 - 2018",
          "Skills", "SQL", "Power BI", "Microsoft Excel", "Data Analysis", "Show all 12 skills",
          "People also viewed", "Somebody Else", "Marketing Manager"]
    return L


def details_lines(p: dict) -> list[str]:
    return ["Experience", p["title"], f"{p['company']} · Full-time", "Mar 2021 - Present · 4 yrs 7 mos",
            p["location"], "Built dashboards in Power BI and wrote complex SQL for MIS reporting.",
            "Data Analyst", "Faysal Bank · Full-time", "Jan 2019 - Feb 2021 · 2 yrs 2 mos", "Karachi, Pakistan",
            "Daily reporting in Excel and SQL.",
            "Junior Analyst", "Meezan Bank · Internship", "Jun 2018 - Dec 2018 · 7 mos", "Karachi, Pakistan",
            "Excel reports.",
            "Data Intern", "HBL · Internship", "Jan 2018 - May 2018 · 5 mos", "Karachi, Pakistan", "Support."]


def _page(lines: list[str]) -> str:
    return ("<!doctype html><html><body><main>"
            + "".join(f"<div>{html.escape(l)}</div>" for l in lines) + "</main></body></html>")


class _H(BaseHTTPRequestHandler):
    server_version = "MockLI/1"

    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body: str, headers: dict | None = None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        st = self.server.state
        u = urlparse(self.path)
        st["hits"].append(u.path + ("?" + u.query if u.query else ""))
        has_cookie = "li_at=" in (self.headers.get("Cookie") or "")

        if u.path.startswith("/login"):
            return self._send(200, _page(["Sign in", "Welcome back"]),
                              {"Set-Cookie": "li_at=mock-session; Path=/; Max-Age=3600"})
        if u.path.startswith("/checkpoint"):
            return self._send(200, _page(["Let's do a quick security check"]))
        if not has_cookie:
            self.send_response(302); self.send_header("Location", "/login"); self.end_headers(); return

        if u.path.startswith("/search/results/people"):
            q = parse_qs(u.query)
            page = int(q.get("page", ["1"])[0])
            st["searches"].append(q.get("keywords", [""])[0])
            if st.get("block_on_search_page") == page:
                self.send_response(302); self.send_header("Location", "/checkpoint/challenge/x"); self.end_headers(); return
            chunk = PEOPLE[(page - 1) * 6: page * 6]
            body = ["<!doctype html><html><body><main><ul>"]
            for p in chunk:
                body.append(
                    f'<li><div><a href="/in/{p["slug"]}/?miniProfileUrn=abc"><span>{p["name"]}</span></a>'
                    f'<div>• 2nd</div><div>{html.escape(p["headline"])}</div>'
                    f'<div>{html.escape(p["location"])}</div><div>Message</div></div></li>')
            body.append("</ul></main></body></html>")
            return self._send(200, "".join(body))

        if u.path.startswith("/in/"):
            parts = [x for x in u.path.split("/") if x]
            slug = parts[1]
            p = next((x for x in PEOPLE if x["slug"] == slug), None)
            if not p:
                return self._send(404, _page(["Not found"]))
            if len(parts) >= 4 and parts[2] == "details":
                st["details"].append(slug)
                return self._send(200, _page(details_lines(p)))
            st["profiles"].append(slug)
            lim = st.get("block_after_profiles")
            if lim is not None and len(st["profiles"]) > lim:
                self.send_response(302); self.send_header("Location", "/checkpoint/challenge/y"); self.end_headers(); return
            return self._send(200, _page(profile_lines(p)))
        return self._send(404, _page(["Not found"]))


class MockLinkedIn:
    def __init__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.srv.state = {"hits": [], "searches": [], "profiles": [], "details": []}
        self.state = self.srv.state
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
