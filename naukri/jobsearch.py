"""Search Naukri for jobs posted in the last 24 hours (read-only - never applies)."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from playwright.sync_api import TimeoutError as PWTimeout

from naukri.browser import NaukriBrowser
from naukri.store import PlainStore

log = logging.getLogger(__name__)

# Friendly shortcuts -> what to search on Naukri
ALIASES = {
    "backend": "backend developer",
    "frontend": "frontend developer",
    "fullstack": "full stack developer",
    "full stack": "full stack developer",
    "devops": "devops engineer",
    "data": "data analyst",
    "testing": "software testing",
    "qa": "qa engineer",
}
# Extra title words that count as a match for a shortcut (besides the keyword itself).
TITLE_TERMS = {
    "backend developer": ["backend", "back end", "node", "java", "python", "golang", "django", "spring", "php", ".net"],
    "frontend developer": ["frontend", "front end", "react", "angular", "vue", "ui developer"],
    "full stack developer": ["full stack", "fullstack", "mern", "mean"],
    "devops engineer": ["devops", "sre", "site reliability", "platform engineer", "cloud engineer"],
    "data analyst": ["data analyst", "data analytics", "business analyst", "power bi", "data scientist"],
    "software testing": ["test", "qa", "quality", "sdet", "automation"],
    "qa engineer": ["qa", "test", "quality", "sdet"],
}
QUICK_PICKS = [("Backend", "backend"), ("Frontend", "frontend"), ("DevOps", "devops"),
               ("Full Stack", "fullstack"), ("Data", "data"), ("Testing", "testing")]


@dataclass
class Job:
    title: str
    company: str
    url: str
    experience: str
    location: str
    posted: str
    salary: str
    tags: list[str]


def parse_query(text: str) -> tuple[str, Optional[int]]:
    """'devops 2' -> ('devops engineer', 2). A trailing number is years of experience."""
    words = text.strip().split()
    exp = None
    if words and words[-1].isdigit() and int(words[-1]) <= 40:
        exp = int(words.pop())
    keyword = " ".join(words).strip()
    return ALIASES.get(keyword.lower(), keyword), exp


def search_url(keyword: str, experience: Optional[int], page_no: int = 1) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-") + "-jobs"
    if page_no > 1:
        slug += f"-{page_no}"
    url = f"https://www.naukri.com/{slug}?k={quote(keyword)}&jobAge=1&sort=f"  # sort=f: newest first
    if experience is not None:
        url += f"&experience={experience}"
    return url


class SearchSession:
    """A browser kept open between searches (a warm search takes ~4 s instead of ~15 s).

    Playwright objects belong to the thread that created them, so every method must be
    called from the bot's single worker thread.
    """

    IDLE_CLOSE_S = 600

    def __init__(self):
        self._nb: Optional[NaukriBrowser] = None
        self.last_used = 0.0

    def page(self):
        if self._nb is None:
            # No login needed for search; a throwaway session keeps users' sessions untouched.
            self._nb = NaukriBrowser(PlainStore(Path("/nonexistent/search-session.json")), fresh=True)
            self._nb.__enter__()
        self.last_used = time.time()
        return self._nb.page

    def idle_for(self) -> float:
        return time.time() - self.last_used if self._nb else 0.0

    def close(self) -> None:
        if self._nb is not None:
            try:
                self._nb.__exit__(None, None, None)
            except Exception as e:
                log.warning("Closing search browser failed: %s", e)
            self._nb = None


_CACHE: dict[tuple[str, Optional[int]], tuple[float, tuple]] = {}
CACHE_S = 600


def find_jobs(keyword: str, experience: Optional[int], limit: int = 15,
              session: Optional[SearchSession] = None) -> tuple[list[Job], Optional[int], bool]:
    """Newest-first jobs from the last 24 h whose title matches, Naukri's total count,
    and whether these are title matches (False = no matches, showing Naukri's results as-is)."""
    key = (keyword.lower(), experience)
    if key in _CACHE and time.time() - _CACHE[key][0] < CACHE_S:
        return _CACHE[key][1]
    own = session is None
    session = session or SearchSession()
    try:
        result = _search(session.page(), keyword, experience, limit)
    except Exception:
        session.close()  # a broken page shouldn't be reused
        raise
    finally:
        if own:
            session.close()
    _CACHE[key] = (time.time(), result)
    return result


def _search(page, keyword: str, experience: Optional[int], limit: int) -> tuple[list[Job], Optional[int], bool]:
    jobs: list[Job] = []
    total = None
    for page_no in (1, 2):
        found, count = _load_results(page, search_url(keyword, experience, page_no))
        if total is None:
            total = count
        jobs += found
        # A second page only when the first had too few relevant jobs.
        if sum(_match_rank(j, keyword) < 2 for j in jobs) >= 8 or len(found) < 15:
            break
    # Newest-first results include loosely related jobs: keep only title matches,
    # unless nothing matches at all.
    # Title matches first (newest first within each group), then skill-tag matches.
    matches = sorted((j for j in jobs if _match_rank(j, keyword) < 2), key=lambda j: _match_rank(j, keyword))
    shown = matches or jobs
    log.info("findjob %r exp=%s: %d matches of %d, total %s", keyword, experience, len(matches), len(jobs), total)
    return shown[:limit], total, bool(matches)


GENERIC_WORDS = {"developer", "engineer", "jobs", "job", "senior", "junior", "and", "for", "in"}


def _load_results(page, url: str) -> tuple[list[Job], Optional[int]]:
    """Jobs on one results page. Reads the job data the page itself receives from Naukri
    (~3 s) instead of waiting for the page to draw it (~10 s more on the small VM)."""
    try:
        with page.expect_response(lambda r: "/jobapi/" in r.url and "/search" in r.url and r.status == 200,
                                  timeout=20_000) as info:
            page.goto(url, wait_until="commit")
        data = info.value.json()
        page.evaluate("window.stop()")  # don't spend CPU drawing a page we won't read
        return [_from_api(j) for j in data.get("jobDetails", []) if j.get("title")], data.get("noOfJobs")
    except Exception as e:
        log.info("Job data not captured (%s) - reading the page instead", str(e).splitlines()[0][:100])
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.locator(".srp-jobtuple-wrapper").first.wait_for(timeout=20_000)
    except PWTimeout:
        return [], None
    found = page.locator(".srp-jobtuple-wrapper").evaluate_all(
        """els => els.map(e => {
            const t = s => { const x = e.querySelector(s); return x ? x.innerText.trim() : ''; };
            const a = e.querySelector('a.title');
            return {title: a ? a.innerText.trim() : '', url: a ? a.href : '',
                    company: t('.comp-name'), experience: t('.expwdth'), location: t('.locWdth'),
                    posted: t('.job-post-day'), salary: t('.sal-wrap span[title], .sal'),
                    tags: [...e.querySelectorAll('.tags-gt li')].map(li => li.innerText.trim())};
        })"""
    )
    return [Job(**j) for j in found if j["title"] and j["url"]], _total_count(page)


def _from_api(j: dict) -> Job:
    labels = {p.get("type"): p.get("label", "") for p in j.get("placeholders") or []}
    url = j.get("jdURL") or ""
    return Job(
        title=j.get("title", "").strip(),
        company=j.get("companyName", "").strip(),
        url=url if url.startswith("http") else "https://www.naukri.com" + url,
        experience=labels.get("experience", ""),
        location=labels.get("location", ""),
        posted=j.get("footerPlaceholderLabel", ""),
        salary=labels.get("salary", ""),
        tags=[t.strip() for t in (j.get("tagsAndSkills") or "").split(",") if t.strip()],
    )


def _total_count(page) -> Optional[int]:
    """Naukri shows e.g. '1 - 20 of 308'; fall back to the page title ('... Jobs - 308 ...')."""
    counter = page.locator("[class*='count-string']").first
    text = counter.inner_text() if counter.count() else ""
    m = re.search(r"of\s+([\d,]+)", text) or re.search(r"Jobs\s*-\s*([\d,]+)", page.title())
    return int(m.group(1).replace(",", "")) if m else None


def _has_term(text: str, term: str) -> bool:
    """Whole-word match: '.net' matches 'Dot Net/.Net' and 'Asp.Net' but not 'Network'."""
    text = re.sub(r"[-_/|()]", " ", text.lower())
    body = r"\s*".join(re.escape(part) for part in term.lower().split())
    before = r"(?<![a-z0-9])" if term[0].isalnum() else ""
    return re.search(before + body + r"(?![a-z0-9])", text) is not None


def _match_rank(job: Job, keyword: str) -> int:
    """0 = title matches, 1 = only a skill tag matches (not for role shortcuts), 2 = no match."""
    terms = TITLE_TERMS.get(keyword.lower())
    if terms:
        return 0 if any(_has_term(job.title, t) for t in terms) else 2
    words = [w for w in keyword.lower().split() if w not in GENERIC_WORDS] or keyword.lower().split()
    if any(_has_term(job.title, w) for w in words):
        return 0
    return 1 if any(_has_term(tag, w) for tag in job.tags for w in words) else 2


def format_jobs(keyword: str, experience: Optional[int], jobs: list[Job], total: Optional[int],
                matched: bool = True) -> str:
    exp_txt = f" · {experience} yrs exp" if experience is not None else ""
    if not jobs:
        return (f"🔎 No <b>{escape(keyword)}</b> jobs posted in the last 24 hours{exp_txt}.\n"
                "Try a different keyword, e.g. <code>/findjob python developer</code>.")
    head = f"🔎 <b>{escape(keyword.title())}</b> jobs posted in the last 24 hours{exp_txt}"
    count = f"Showing {len(jobs)} newest" + (f" · Naukri has {total:,} in total" if total else "")
    lines = [head, f"<i>{count}</i>", ""]
    if not matched:
        lines[2:2] = ["⚠️ <i>No job titles matched exactly - these are Naukri's closest results.</i>"]
    for i, j in enumerate(jobs, 1):
        details = " · ".join(x for x in (f"💼 {j.experience}" if j.experience else "",
                                          f"📍 {j.location}" if j.location else "",
                                          f"🕒 {j.posted}" if j.posted else "") if x)
        salary = f"\n   💰 {escape(j.salary)}" if j.salary and "not disclosed" not in j.salary.lower() else ""
        lines.append(f"{i}. <a href=\"{escape(j.url, quote=True)}\">{escape(j.title)}</a> - {escape(j.company)}\n"
                     f"   {escape(details)}{salary}")
    lines += ["", f"<a href=\"{escape(search_url(keyword, experience), quote=True)}\">See all results on Naukri →</a>"]
    return "\n".join(lines)
