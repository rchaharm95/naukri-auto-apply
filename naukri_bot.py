import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, Page

logger = logging.getLogger(__name__)

NAUKRI_BASE    = "https://www.naukri.com"
LOGIN_URL      = f"{NAUKRI_BASE}/nlogin/login"
PROFILE_URL    = f"{NAUKRI_BASE}/mnjuser/profile"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Chatbot question patterns -> profile key to answer with
QUESTION_MAP = [
    (r"current\s+ctc|current\s+salary|current\s+package",    "current_ctc"),
    (r"expected\s+ctc|expected\s+salary|expected\s+package",  "expected_ctc"),
    (r"notice\s+period|joining\s+time|available\s+to\s+join", "notice_period"),
    (r"current\s+(city|location)",                            "location"),
    (r"year[s]?\s+of\s+exp|total\s+exp|how\s+many\s+year",   "experience"),
    (r"current\s+company|current\s+employer|current\s+org",   "current_company"),
    (r"current\s+designation|current\s+role|current\s+title", "current_designation"),
    (r"comfortable|ok\s+with|agree|confirm",                  "yes"),
    (r"relocat",                                              "relocation"),
    (r"work\s+from\s+(home|office)|wfh|hybrid",               "work_mode"),
]


class NaukriBot:
    def __init__(self, config: dict):
        self.config   = config
        self.creds    = config["credentials"]
        self.search_cfg = config["search"]
        self.settings = config["settings"]
        self.log_file = Path("applied_jobs.json")
        self.jd_log_file = Path("jd_logs.jsonl")
        self.answers_file = Path("answers.json")
        self.applied_jobs: dict = self._load_log()
        self.custom_answers: dict = self._load_answers()
        self.profile: dict = {}
        self.resume_path: str = ""
        self._playwright = None
        self.browser = None
        self.context = None
        self.page: Page = None

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _load_log(self) -> dict:
        if self.log_file.exists():
            try:
                return json.loads(self.log_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_log(self):
        self.log_file.write_text(
            json.dumps(self.applied_jobs, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load_answers(self) -> dict:
        if self.answers_file.exists():
            try:
                return json.loads(self.answers_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {}

    def _save_answers(self):
        self.answers_file.write_text(
            json.dumps(self.custom_answers, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ #
    #  Browser lifecycle                                                   #
    # ------------------------------------------------------------------ #

    async def start(self):
        self._playwright = await async_playwright().start()
        try:
            self.browser = await self._playwright.chromium.launch(
                headless=self.settings.get("headless", False),
                slow_mo=self.settings.get("slow_mo", 800),
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            logger.info(f"Failed to launch default chromium ({e}). Trying system Chrome channel...")
            self.browser = await self._playwright.chromium.launch(
                channel="chrome",
                headless=self.settings.get("headless", False),
                slow_mo=self.settings.get("slow_mo", 800),
                args=["--disable-blink-features=AutomationControlled"],
            )
        self.context = await self.browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            accept_downloads=True,
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self.page = await self.context.new_page()

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------ #
    #  Login                                                               #
    # ------------------------------------------------------------------ #

    async def login(self) -> bool:
        logger.info("Navigating to login page ...")
        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await self._sleep(2)

        try:
            try:
                cookie_btn = self.page.locator("button:has-text('Accept')")
                if await cookie_btn.count() > 0:
                    await cookie_btn.first.click()
                    await self._sleep(1)
            except Exception:
                pass

            email_input = self.page.locator(
                'input[placeholder*="Email"], input[type="email"], #usernameField'
            ).first
            await email_input.wait_for(state="visible", timeout=10000)
            await email_input.fill(self.creds["email"])

            pwd_input = self.page.locator(
                'input[type="password"], input[placeholder*="password"]'
            ).first
            await pwd_input.fill(self.creds["password"])

            submit_btn = self.page.locator(
                'button[type="submit"], button:has-text("Login")'
            ).first
            await submit_btn.click()

            await self.page.wait_for_function(
                "() => !window.location.href.includes('/nlogin')",
                timeout=60000,
            )
            logger.info("Login successful.")
            return True

        except PlaywrightTimeout:
            logger.error("Login timed out -- check credentials or CAPTCHA.")
            return False
        except Exception as exc:
            logger.error(f"Login error: {exc}")
            return False

    # ------------------------------------------------------------------ #
    #  Profile fetch                                                       #
    # ------------------------------------------------------------------ #

    async def fetch_profile(self):
        """Read the logged-in user's profile from Naukri and store in self.profile."""
        logger.info("Fetching your Naukri profile ...")
        try:
            await self.page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=30000)
            await self._sleep(3)

            async def _text(selector: str) -> str:
                try:
                    el = self.page.locator(selector).first
                    if await el.count() > 0:
                        return (await el.inner_text()).strip()
                except Exception:
                    pass
                return ""

            # --- Name ---
            name = await _text(".name, .nameHead, h1.nName, span.nI-gNb-drawer__name")

            # --- Experience ---
            exp_raw = await _text(
                "[class*='exp'] span, .exp-years, "
                "span[class*='experienceHead'], .expValue"
            )
            exp_years = 0
            m = re.search(r"(\d+)", exp_raw)
            if m:
                exp_years = int(m.group(1))

            # --- Current company & designation ---
            company = await _text(
                ".curr-company, .currentCompany, "
                "[class*='company'] span, .orgName"
            )
            designation = await _text(
                ".curr-designation, .designation, "
                "[class*='designation'] span, .desig"
            )

            # --- Location ---
            location = await _text(
                ".location, .curr-location, "
                "[class*='location'] span, .locValue"
            )
            if not location:
                location = self.search_cfg.get("location", "Pune")

            # --- CTC (current & expected) ---
            ctc_raw = await _text(
                ".ctc-lpa, .curr-salary, [class*='salary'] span, .salaryValue"
            )
            exp_ctc_raw = await _text(
                ".exp-salary, [class*='expectedSalary'] span, .expSalValue"
            )

            # --- Notice period ---
            notice_raw = await _text(
                ".notice-period, .noticeValue, .noticePeriod, span.notice-period"
            )

            # --- Phone ---
            phone_raw = await _text(
                ".mobile-number, .mobileNumber, .phone, "
                "[class*='mobile'] span, [class*='phone'] span, .mobValue"
            )

            # Try reading the full profile text as fallback for missing fields
            page_text = await self.page.inner_text("body")

            def _extract(pattern: str, text: str) -> str:
                m = re.search(pattern, text, re.IGNORECASE)
                return m.group(1).strip() if m else ""

            if not ctc_raw:
                ctc_raw = _extract(r"current\s+(?:ctc|salary)[:\s]+([^\n,]+)", page_text)
            if not exp_ctc_raw:
                exp_ctc_raw = _extract(r"expected\s+(?:ctc|salary)[:\s]+([^\n,]+)", page_text)
            if not notice_raw:
                notice_raw = _extract(r"notice\s+period[:\s]+([^\n,]+)", page_text)
            if not company:
                company = _extract(r"current\s+company[:\s]+([^\n,]+)", page_text)
            if not designation:
                designation = _extract(r"designation[:\s]+([^\n,]+)", page_text)
            if not name:
                name = _extract(r"(?:hello|hi)[,\s]+([A-Za-z ]+)", page_text)
            if not phone_raw:
                # Match +91 9XXXXXXX or plain 10-digit mobile numbers
                m = re.search(r"(\+91[\s\-]?\d{10}|\b[6-9]\d{9}\b)", page_text)
                phone_raw = m.group(1).strip() if m else ""

            self.profile = {
                "name":                 name or "",
                "experience":           str(exp_years) if exp_years else str(self.search_cfg.get("experience", 3)),
                "current_company":      company or "",
                "current_designation":  designation or "",
                "location":             location,
                "current_ctc":          ctc_raw or "As per company norms",
                "expected_ctc":         exp_ctc_raw or "As per industry standards",
                "notice_period":        notice_raw or "30-45 days",
                "phone":                phone_raw or "",
                "relocation":           "Yes" if self.settings.get("open_to_relocate", True) else "No",
                "work_mode":            self.search_cfg.get("work_mode", "hybrid"),
                "yes":                  "Yes",
            }

            logger.info(f"Profile loaded: {name or 'N/A'} | {company or 'N/A'} | {ctc_raw or 'N/A'}")
            logger.info(f"  Experience  : {self.profile['experience']} yrs")
            logger.info(f"  Location    : {self.profile['location']}")
            logger.info(f"  Notice      : {self.profile['notice_period']}")
            logger.info(f"  Phone       : {phone_raw or 'N/A'}")

            self.resume_path = await self._download_resume()
            if self.resume_path:
                cv_data = self._parse_cv(self.resume_path)
                self.profile.update(cv_data)

        except Exception as exc:
            logger.warning(f"Could not fully read profile ({exc}). Using defaults from config.")
            self.profile = {
                "name":                "",
                "experience":          str(self.search_cfg.get("experience", 3)),
                "current_company":     "",
                "current_designation": "",
                "location":            self.search_cfg.get("location", "Pune"),
                "current_ctc":         "As per company norms",
                "expected_ctc":        "As per industry standards",
                "notice_period":       "30-45 days",
                "phone":               "",
                "relocation":          "Yes",
                "work_mode":           "Hybrid / Work from Home",
                "yes":                 "Yes",
            }

    async def _download_resume(self) -> str:
        """Download CV from Naukri profile. Reuses cached copy if already downloaded."""
        save_dir = Path("resume_download")
        save_dir.mkdir(exist_ok=True)

        # Reuse already-downloaded resume from a previous run
        existing = sorted(save_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if existing:
            logger.info(f"  Using cached resume: {existing[0]}")
            return str(existing[0])

        # Try to download fresh from Naukri profile page
        try:
            SELECTORS = [
                'a:has-text("Download Resume")',
                'button:has-text("Download Resume")',
                'a:has-text("Download")',
                '[class*="downloadResume"]',
                '[class*="download-resume"]',
                'a[title*="download" i]',
                'a[href*="resume"][download]',
                '[data-ga-track*="resume" i]',
                '.resumeHead a', '.resume-head a',
            ]
            for sel in SELECTORS:
                dl_btn = self.page.locator(sel).first
                if await dl_btn.count() > 0:
                    async with self.page.expect_download(timeout=20000) as dl_info:
                        await dl_btn.click()
                    download = await dl_info.value
                    save_path = save_dir / download.suggested_filename
                    await download.save_as(str(save_path))
                    logger.info(f"  Resume downloaded: {save_path}")
                    return str(save_path)

            logger.info("  Resume button not found on profile page — upload skipped on company sites")
            return ""
        except Exception as exc:
            logger.warning(f"  Resume download failed: {exc}")
            return ""

    def _parse_cv(self, pdf_path: str) -> dict:
        """Extract structured data from resume PDF and merge into profile."""
        text = ""
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                for pg in pdf.pages:
                    text += (pg.extract_text() or "") + "\n"
        except ImportError:
            logger.warning("pdfplumber not installed — run: pip install pdfplumber")
            return {}
        except Exception as exc:
            logger.warning(f"CV parse error: {exc}")
            return {}

        if not text.strip():
            return {}

        data = {}

        # LinkedIn / GitHub
        m = re.search(r'linkedin\.com/in/([\w\-]+)', text, re.IGNORECASE)
        if m:
            data["linkedin"] = f"https://www.linkedin.com/in/{m.group(1)}"

        m = re.search(r'github\.com/([\w\-]+)', text, re.IGNORECASE)
        if m:
            data["github"] = f"https://github.com/{m.group(1)}"

        # Highest degree
        for pat, label in [
            (r'\bM\.?Tech\b|\bMaster of Technology\b', "M.Tech"),
            (r'\bMBA\b', "MBA"),
            (r'\bMCA\b', "MCA"),
            (r'\bM\.?Sc\b', "M.Sc"),
            (r'\bB\.?Tech\b|\bBachelor of Technology\b|\bBE\b|\bB\.E\b', "B.Tech"),
            (r'\bBCA\b', "BCA"),
            (r'\bB\.?Sc\b', "B.Sc"),
        ]:
            if re.search(pat, text, re.IGNORECASE):
                data["degree"] = label
                break

        # University / College name
        m = re.search(
            r'(?:university|college|institute)[^\n\-:]{0,5}[:\-]?\s*([A-Z][^\n,]{5,60})',
            text, re.IGNORECASE,
        )
        if m:
            data["university"] = m.group(1).strip()

        # Graduation year — earliest 20xx year found
        years = re.findall(r'\b(20\d{2})\b', text)
        if years:
            data["graduation_year"] = sorted(years)[0]

        # Skills section
        m = re.search(
            r'(?:skills?|technical\s+skills?|technologies)[^\n]*\n(.*?)(?:\n\n|\n[A-Z][A-Z]|\Z)',
            text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            raw = re.sub(r'[•\-\*\|]', ',', m.group(1))
            # Remove category prefixes like "Frontend:", "Backend:"
            raw = re.sub(r'\b[a-zA-Z\s&/]+:\s*', '', raw)
            skills = re.sub(r'\s+', ' ', raw).strip().strip(',')
            if skills:
                data["skills"] = skills[:400]

        logger.info(f"  CV fields extracted: {list(data.keys())}")
        return data

    # ------------------------------------------------------------------ #
    #  Job search                                                          #
    # ------------------------------------------------------------------ #

    def _build_search_url(self, page_num: int = 1, keyword: str = None) -> str:
        custom_url = self.search_cfg.get("custom_url")
        if custom_url:
            if page_num == 1:
                return custom_url
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(custom_url)
            path = parsed.path.rstrip("/")
            path = f"{path}-{page_num}"
            return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))

        kw_raw = keyword or self.search_cfg.get("keyword", "")
        loc_raw = self.search_cfg.get("location", "")
        
        kw_slug = re.sub(r'[^a-zA-Z0-9]+', '-', kw_raw.strip()).strip('-').lower()
        loc_slug = re.sub(r'[^a-zA-Z0-9]+', '-', loc_raw.strip()).strip('-').lower()
        
        path = f"/{kw_slug}-jobs" if kw_slug else "/jobs"
        if loc_slug:
            path += f"-in-{loc_slug}"
            
        if page_num > 1:
            path += f"-{page_num}"
            
        params = []
        exp = self.search_cfg.get("experience", 0)
        if exp:
            params.append(f"experience={exp}")
            
        sal = self.search_cfg.get("salary_min", 0)
        if sal:
            params.append(f"salary={sal}")
            
        # Use sortBy=date for date sorting on SEO slugs
        params.append("sortBy=date")
        
        job_age = self.search_cfg.get("job_age")
        if job_age:
            params.append(f"jobAge={job_age}")
            
        work_mode = self.search_cfg.get("work_mode", "")
        if work_mode:
            mode_map = {"work from home": "1", "wfh": "1", "hybrid": "2", "office": "3"}
            codes = [mode_map[m.strip().lower()] for m in work_mode.split(",")
                     if m.strip().lower() in mode_map]
            if codes:
                params.append(f"wfhType={quote_plus(','.join(codes))}")
                
        if self.search_cfg.get("gcc_only", False):
            params.append("companyType=2")
            
        query = "?" + "&".join(params) if params else ""
        return f"{NAUKRI_BASE}{path}{query}"

    CARD_SELECTORS  = [
        # Current Naukri design (2024-25)
        "div.srp-jobtuple-wrapper",
        "div[data-job-id]",
        "article[data-job-id]",
        # Older selectors kept as fallback
        "article.jobTupleHeader",
        ".jobTuple",
        ".cust-job-tuple",
        "div[class*='jobTuple']",
        "article[class*='jobTuple']",
    ]
    TITLE_SELECTORS   = [
        "a.title", ".title a", "a[class*='title']",
        ".jobTitle a", "h2 a", "a[href*='/job-listings-']",
    ]
    COMPANY_SELECTORS = [
        ".comp-name", ".companyName", ".company-name",
        "a[class*='comp']", "span.comp-name",
    ]
    EXP_SELECTORS     = [
        ".exp-wrap", "span.exp", ".experience", "li.exp",
        "span[class*='exp']", ".expwdth", ".jobExp",
    ]

    def _parse_exp_range(self, exp_text: str):
        """Return (min_years, max_years) from '3 - 6 Yrs' or '5+ Yrs'. None if unparseable."""
        numbers = list(map(int, re.findall(r"\d+", exp_text)))
        if not numbers:
            return None, None
        return numbers[0], numbers[1] if len(numbers) > 1 else None

    async def _extract_jobs_from_page(self) -> tuple[list[dict], int]:
        jobs = []
        await self._sleep(2)

        cards = []
        for sel in self.CARD_SELECTORS:
            found = await self.page.query_selector_all(sel)
            if found:
                logger.info(f"Matched card selector: {sel} ({len(found)} cards)")
                cards = found
                break

        if not cards:
            logger.warning("Standard selectors failed -- trying fallback link scan.")
            anchors = await self.page.query_selector_all("a[href*='naukri.com'][href*='-jd-']")
            if not anchors:
                anchors = await self.page.query_selector_all("a[href*='/job-listings-']")
            for a in anchors:
                try:
                    title  = (await a.inner_text()).strip()
                    url    = await a.get_attribute("href") or ""
                    if url.startswith("//"):
                        url = "https:" + url
                    job_id = url.rstrip("/").split("?")[0].split("-")[-1]
                    if title and job_id and job_id not in self.applied_jobs:
                        jobs.append({"id": job_id, "title": title, "company": "Unknown", "url": url})
                except Exception:
                    pass
            return jobs, len(anchors) if anchors else 0

        for card in cards:
            try:
                title_el = None
                for sel in self.TITLE_SELECTORS:
                    title_el = await card.query_selector(sel)
                    if title_el:
                        break
                if not title_el:
                    continue

                title  = (await title_el.inner_text()).strip()
                title_lower = title.lower()
                
                # Initial Level Title Filter to prevent scanning irrelevant jobs
                # Skip known irrelevant roles if we are looking for backend/java
                irrelevant_terms = ["qa ", "quality analyst", "test", "testing", "hr ", "sales", "marketing", "support", "frontend", "front end", "ui/", "ui developer", "ux", "data engineer", "data scientist", "ai/ml", "shopify", "drupal", "magento", "wordpress", "php", "node", "python", "business", "analyst", "cad ", "autocad", "mechanical", "electrical", "civil", "network engineer"]
                if any(term in title_lower for term in irrelevant_terms):
                    logger.debug(f"Skipping completely irrelevant title: {title}")
                    continue
                    
                # Strict Title Filter (enforce java/springboot only if explicitly enabled)
                if self.settings.get("strict_title_match", False):
                    if "java" not in title_lower and "springboot" not in title_lower and "spring boot" not in title_lower and "backend" not in title_lower and "software" not in title_lower and "full stack" not in title_lower:
                        logger.debug(f"Skipping (Title missing core keywords): {title}")
                        continue

                url    = await title_el.get_attribute("href") or ""
                if url.startswith("//"):
                    url = "https:" + url

                job_id = url.rstrip("/").split("?")[0].split("-")[-1]

                company_el = None
                for sel in self.COMPANY_SELECTORS:
                    company_el = await card.query_selector(sel)
                    if company_el:
                        break
                company = (await company_el.inner_text()).strip() if company_el else "Unknown"

                # Filter by experience requirement
                exp_el = None
                for sel in self.EXP_SELECTORS:
                    exp_el = await card.query_selector(sel)
                    if exp_el:
                        break
                if exp_el:
                    exp_text = (await exp_el.inner_text()).strip()
                    min_req, _ = self._parse_exp_range(exp_text)
                    user_exp = self.search_cfg.get("experience", 0)
                    # Allow up to 2 years above user's experience as tolerance
                    if min_req is not None and min_req > user_exp + 2:
                        logger.debug(
                            f"Skipping (requires {min_req}+ yrs, you have {user_exp}): {title}"
                        )
                        continue

                if job_id in self.applied_jobs:
                    logger.debug(f"Already applied -- skipping: {title}")
                    continue

                jobs.append({"id": job_id, "title": title, "company": company, "url": url})
            except Exception as exc:
                logger.debug(f"Card parse error: {exc}")

        return jobs, len(cards)

    # Selector string used to wait for job cards to paint (JS-rendered)
    _CARD_WAIT_SEL = (
        "div.srp-jobtuple-wrapper, div[data-job-id], article[data-job-id], "
        ".jobTuple, a[href*='/job-listings-']"
    )

    async def search_jobs(self) -> list[dict]:
        max_pages = self.settings.get("max_pages", 3)
        all_jobs: list[dict] = []
        seen_ids: set = set()

        if self.search_cfg.get("custom_url"):
            keywords = ["CUSTOM_URL"]
        else:
            # Support both "keywords" (list) and legacy "keyword" (single string)
            keywords = (
                self.search_cfg.get("keywords")
                or [self.search_cfg.get("keyword", "")]
            )

        for keyword in keywords:
            logger.info(f"==> Keyword search: '{keyword}'")
            for page_num in range(1, max_pages + 1):
                url = self._build_search_url(page_num, keyword=keyword)
                logger.info(f"  Page {page_num}: {url}")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Wait for JS-rendered job cards to appear before scraping
                try:
                    await self.page.wait_for_selector(self._CARD_WAIT_SEL, timeout=12000)
                except Exception:
                    logger.warning("  Job card wait timed out — trying anyway.")
                await self._sleep(2)

                page_jobs, card_count = await self._extract_jobs_from_page()
                new_jobs  = [j for j in page_jobs if j["id"] not in seen_ids]
                seen_ids.update(j["id"] for j in new_jobs)
                logger.info(f"  -> {len(new_jobs)} new jobs on page {page_num}")
                all_jobs.extend(new_jobs)

                if card_count == 0:
                    logger.info("  No more jobs on this keyword — next keyword.")
                    break

        logger.info(f"Total new jobs to process: {len(all_jobs)}")
        return all_jobs

    # ------------------------------------------------------------------ #
    #  Apply                                                               #
    # ------------------------------------------------------------------ #

    async def apply_to_job(self, job: dict) -> bool:
        try:
            await self.page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
            await self._sleep(3)

            # Extract JD text before doing anything else
            try:
                jd_text_raw = ""
                for sel in [".dang-inner-html", ".job-desc", "section.job-desc", "[class*='job-description']", "[class*='Jd__']", "main"]:
                    els = await self.page.query_selector_all(sel)
                    for el in els:
                        text = (await el.inner_text()).strip()
                        if text:
                            jd_text_raw = text
                            break
                    if jd_text_raw:
                        break
                job["jd_text"] = jd_text_raw.strip()
            except Exception as e:
                logger.warning(f"Failed to read JD: {e}")
                job["jd_text"] = ""

            apply_btn = None
            for selector in [
                'button.apply-button',
                'button[class*="apply"]',
                'a[class*="apply"]',
                'button:has-text("Apply")',
            ]:
                locator = self.page.locator(selector).first
                if await locator.count() > 0:
                    apply_btn = locator
                    break

            if apply_btn is None:
                logger.warning(f"No apply button found: {job['title']}")
                self._log_job(job, "no_apply_button")
                return False

            btn_text = (await apply_btn.inner_text()).lower()
            if self.settings.get("skip_company_site_jobs", True) and "company" in btn_text:
                logger.info(f"Skipping company-site job: {job['title']}")
                self._log_job(job, "skipped_company_site")
                return False

            # JD Skill matching
            try:
                jd_text = job.get("jd_text", "").lower()
                
                # Mandatory core skills check
                if jd_text:
                    if not any(k in jd_text for k in ["java", "springboot", "spring boot"]):
                        logger.info(f"Skipping job due to missing mandatory skill (Java/Spring Boot): {job['title']}")
                        self._log_job(job, "skipped_skills_mismatch_missing_core")
                        return False

                user_skills_raw = self.profile.get("skills", "")
                if user_skills_raw:
                    # Clean up category prefixes if any exist
                    clean_raw = re.sub(r'\b[a-zA-Z\s&/]+:\s*', '', user_skills_raw)
                    user_skills = [s.strip().lower() for s in clean_raw.split(",") if s.strip()]
                    
                    match_count = 0
                    for skill in user_skills:
                        escaped = re.escape(skill)
                        # Use word boundaries for better matching if alphanumeric
                        pattern = r'\b' + escaped + r'\b' if skill and skill[-1].isalnum() and skill[0].isalnum() else escaped
                        if re.search(pattern, jd_text):
                            match_count += 1
                    
                    if match_count == 0 and len(user_skills) > 0:
                        logger.info(f"Skipping job due to skill mismatch (0 matches): {job['title']}")
                        self._log_job(job, "skipped_skills_mismatch")
                        return False
            except Exception as e:
                logger.warning(f"Failed to read JD for skill match: {e}")

            logger.info(f"Applying to: {job['title']} @ {job['company']}")
            pages_before = len(self.page.context.pages)
            await apply_btn.click()
            await self._sleep(3)  # wait for modal OR new tab to appear

            # If a new tab opened, it's an external company site
            if len(self.page.context.pages) > pages_before:
                new_tab = self.page.context.pages[-1]
                if self.settings.get("skip_company_site_jobs", True):
                    await new_tab.close()
                    logger.info("  Opened external site — skipping.")
                    self._log_job(job, "skipped_company_site")
                    return False
                else:
                    logger.info("  Opened external site — attempting to apply ...")
                    applied = await self._apply_on_company_site(new_tab)
                    self._log_job(job, "company_site_applied" if applied else "company_site_failed")
                    return applied

            # No new tab — this is a Naukri-native apply
            if self.settings.get("only_company_site_jobs", False):
                logger.info(f"  Skipping Naukri native apply (only company site mode)")
                self._log_job(job, "skipped_naukri_native")
                return False

            applied = await self._handle_apply_flow()
            status = "applied" if applied else "apply_flow_incomplete"
            self._log_job(job, status)

            if applied:
                logger.info("  [OK] Applied successfully")
            else:
                logger.warning("  [!] Apply flow did not complete cleanly")
            return applied

        except Exception as exc:
            msg = str(exc)
            if "closed" in msg.lower():
                logger.warning(f"  Tab/browser closed during apply — skipping: {job['title']}")
                self._log_job(job, "skipped_tab_closed")
            else:
                logger.error(f"Error applying to {job['title']}: {exc}")
                self._log_job(job, "error")
            return False

    # ------------------------------------------------------------------ #
    #  Apply flow handler (chatbot + modals)                              #
    # ------------------------------------------------------------------ #

    async def _fill_naukri_modal_form(self):
        """Scan and fill all inputs in a multi-question Naukri modal."""
        modal = self.page.locator("[class*='modal'], [class*='chatbot'], [class*='apply']").first
        if await modal.count() == 0:
            modal = self.page
            
        fields = modal.locator('input:visible, select:visible, textarea:visible')
        count = await fields.count()
        for i in range(count):
            field = fields.nth(i)
            tag = await field.evaluate("el => el.tagName.toLowerCase()")
            type_attr = await field.evaluate("el => el.type") if tag == 'input' else ''
            
            if type_attr in ('hidden', 'submit', 'button'):
                continue
                
            q_text = await field.evaluate("""el => {
                if (el.id) {
                    let l = document.querySelector(`label[for="${el.id}"]`);
                    if(l) return l.innerText;
                }
                if (el.placeholder) return el.placeholder;
                return el.parentElement ? el.parentElement.innerText.split('\\n')[0] : '';
            }""")
            
            q_text = (q_text or "").strip()
            if not q_text:
                continue
                
            ans = self._resolve_answer(q_text)
            
            try:
                if tag == "select":
                    await field.select_option(label=ans)
                elif type_attr in ('radio', 'checkbox'):
                    val = await field.get_attribute("value")
                    if val and ans.lower() in val.lower():
                        await field.click()
                else:
                    await field.fill(ans)
            except Exception:
                pass

    async def _handle_apply_flow(self) -> bool:
        await self._sleep(2)
        
        # Try filling multi-question modal first
        try:
            await self._fill_naukri_modal_form()
            await self._sleep(1)
        except Exception:
            pass

        if await self._is_success():
            return True

        # Case 1: Simple one-click modal (Submit / Apply Now / Confirm)
        for selector in [
            'button:has-text("Submit")', 'button:has-text("Apply Now")',
            'button:has-text("Confirm")', 'button:has-text("Apply")',
        ]:
            try:
                btn = self.page.locator(selector).first
                if await btn.count() > 0:
                    await btn.click(timeout=5000)
                    await self._sleep(3)
                    if await self._is_success():
                        return True
            except Exception:
                pass

        # Case 2: Chatbot / multi-step question flow
        for step in range(25):
            await self._sleep(2)

            if await self._is_success():
                return True

            try:
                await self._answer_chatbot_question()
            except Exception:
                pass

            clicked = False
            for sel in [
                'button[class*="send"]', 'button[class*="next"]',
                'button:has-text("Send")', 'button:has-text("Next")',
                'button:has-text("Continue")', 'button:has-text("Submit")',
                'button:has-text("Done")', 'button:has-text("Apply")',
            ]:
                try:
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click(timeout=5000)
                        clicked = True
                        await self._sleep(2)
                        break
                except Exception:
                    pass

            if await self._is_success():
                return True

            if not clicked:
                break

        # Treat as success if no error was raised — Naukri sometimes closes modal silently
        return True

    # ------------------------------------------------------------------ #
    #  Stuck popup                                                         #
    # ------------------------------------------------------------------ #

    async def _show_stuck_popup(self, page, message: str) -> bool:
        """
        Show a minimal popup — buttons at TOP so always visible.
        Continue = bot resumes. Skip = move to next job.
        Returns True to continue, False to skip.
        """
        try:
            await page.evaluate("""
                (msg) => {
                    const ex = document.getElementById('_nb_popup');
                    if (ex) ex.remove();
                    window._nb_action = 'continue';

                    const ov = document.createElement('div');
                    ov.id = '_nb_popup';
                    ov.style.cssText = 'position:fixed;top:16px;right:16px;z-index:2147483647;';

                    const box = document.createElement('div');
                    box.style.cssText = 'background:#1a202c;color:#fff;padding:16px 18px;border-radius:10px;'
                        + 'width:300px;font-family:Arial,sans-serif;box-shadow:0 4px 20px rgba(0,0,0,.5);';
                    box.innerHTML =
                        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">'
                        + '<span style="font-size:22px;">🤖</span>'
                        + '<b style="color:#fc8181;font-size:14px;">Bot needs help!</b>'
                        + '</div>'
                        + '<p style="font-size:12px;color:#e2e8f0;margin:0 0 12px;line-height:1.5;">' + msg + '</p>'
                        + '<div style="display:flex;gap:8px;">'
                        + '<button id="_nb_ok" style="flex:2;background:#48bb78;color:#fff;border:none;'
                        + 'padding:9px 0;border-radius:6px;font-size:13px;font-weight:bold;cursor:pointer;">'
                        + '✅ Continue</button>'
                        + '<button id="_nb_skip" style="flex:1;background:#fc8181;color:#fff;border:none;'
                        + 'padding:9px 0;border-radius:6px;font-size:13px;cursor:pointer;">'
                        + '⏭ Skip</button>'
                        + '</div>';

                    ov.appendChild(box);
                    document.body.appendChild(ov);

                    document.getElementById('_nb_ok').onclick = () => {
                        window._nb_action = 'continue';
                        ov.remove();
                    };
                    document.getElementById('_nb_skip').onclick = () => {
                        window._nb_action = 'skip';
                        ov.remove();
                    };
                }
            """, message.replace("\n", " "))

            logger.info("  [POPUP] Showing — waiting for user...")
            await page.wait_for_selector("#_nb_popup", state="detached", timeout=300000)
            action = await page.evaluate("() => window._nb_action || 'continue'")
            logger.info(f"  [POPUP] User chose: {action}")
            return action != "skip"

        except Exception as exc:
            logger.warning(f"  Popup error: {exc}")
            return True

    async def _has_captcha(self, page) -> bool:
        """Return True if a CAPTCHA is visible on the page."""
        for sel in [
            "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
            ".g-recaptcha", "#captcha", "[class*='captcha']", "[id*='captcha']",
        ]:
            try:
                if await page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------ #
    #  Company site apply                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_company_site_auth(self, page) -> None:
        """Log in or sign up on a company site using Naukri credentials."""
        email    = self.creds.get("email", "")
        password = self.creds.get("password", "")
        name_parts = self.profile.get("name", "").split()
        first_name = name_parts[0] if name_parts else "Applicant"
        last_name  = name_parts[-1] if len(name_parts) > 1 else "User"

        await self._sleep(2)
        email_inp = page.locator(
            'input[type="email"], input[name*="email" i], input[placeholder*="email" i]'
        ).first
        if await email_inp.count() == 0:
            return  # No auth form present

        try:
            await email_inp.scroll_into_view_if_needed()
            try:
                await email_inp.wait_for(state="visible", timeout=5000)
            except Exception:
                pass  # Try force-fill anyway via JS below
            await email_inp.fill(email, timeout=5000)
            await self._sleep(1)

            # Step 1 — click Continue / Next if present (some sites split email + password)
            for sel in [
                'button:has-text("Continue")', 'button:has-text("Next")',
                'button:has-text("Sign in")', 'button[type="submit"]',
            ]:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    await self._sleep(2)
                    break

            # Step 2 — fill password (login or signup)
            pwd_inp = page.locator('input[type="password"]').first
            if await pwd_inp.count() > 0:
                await pwd_inp.scroll_into_view_if_needed()
                await pwd_inp.wait_for(state="visible", timeout=5000)
                await pwd_inp.fill(password, timeout=5000)

                # Confirm password field means it's a signup form
                confirm_inp = page.locator(
                    'input[placeholder*="confirm" i], input[name*="confirm" i], input[id*="confirm" i]'
                ).first
                if await confirm_inp.count() > 0:
                    await confirm_inp.fill(password)
                    # Fill name fields on signup
                    for name_sel, val in [
                        ('input[placeholder*="first name" i], input[name*="firstName" i]', first_name),
                        ('input[placeholder*="last name" i], input[name*="lastName" i]',  last_name),
                    ]:
                        inp = page.locator(name_sel).first
                        if await inp.count() > 0:
                            try:
                                await inp.fill(val)
                            except Exception:
                                pass

                # Submit auth form
                for sel in [
                    'button[type="submit"]', 'button:has-text("Sign in")',
                    'button:has-text("Log in")', 'button:has-text("Create account")',
                    'button:has-text("Sign up")', 'button:has-text("Register")',
                ]:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click()
                        await self._sleep(3)
                        break

            logger.info("  Auth handled on company site")
        except Exception as exc:
            logger.warning(f"  Auth error on company site: {exc}")

    async def _fill_company_form(self, page) -> None:
        """Fill every visible form field on a company career page using CV + Naukri profile."""
        p = self.profile
        email      = self.creds.get("email", "")
        name_parts = p.get("name", "").split()
        first_name = name_parts[0] if name_parts else ""
        last_name  = name_parts[-1] if len(name_parts) > 1 else ""
        full_name  = p.get("name", "")
        exp        = p.get("experience", "4")
        cover_letter = (
            f"Hi, I am {full_name}, a .NET Full Stack Developer with {exp} years of experience. "
            f"Currently working at {p.get('current_company', 'my current company')} as "
            f"{p.get('current_designation', 'a developer')}. "
            f"Skills: {p.get('skills', '.NET Core, C#, Angular, React, SQL')}. "
            f"Notice period: {p.get('notice_period', '30 days')}. "
            f"Looking for hybrid / work-from-home opportunities in Pune."
        )

        # (selector, value) — only fills if the field is empty
        field_map = [
            # Identity
            ('input[type="email"], input[placeholder*="email" i], input[name*="email" i]',              email),
            ('input[placeholder*="first name" i], input[name*="firstName" i], input[name*="first_name" i]', first_name),
            ('input[placeholder*="last name" i],  input[name*="lastName" i],  input[name*="last_name" i]',  last_name),
            ('input[placeholder*="full name" i],  input[placeholder*="your name" i], input[name="name"]',   full_name),
            ('input[placeholder*="phone" i], input[type="tel"], input[name*="phone" i], input[name*="mobile" i]', p.get("phone", "")),
            ('input[placeholder*="city" i],     input[name*="city" i],     input[placeholder*="location" i]', p.get("location", "")),

            # Online profiles
            ('input[placeholder*="linkedin" i], input[name*="linkedin" i]', p.get("linkedin", "")),
            ('input[placeholder*="github" i],   input[name*="github" i]',   p.get("github", "")),

            # Work experience
            ('input[placeholder*="year.*exp" i], input[placeholder*="total.*exp" i], input[name*="totalExp" i], input[name*="experience" i]', exp),
            ('input[placeholder*="current.*company" i],     input[name*="currentCompany" i],     input[placeholder*="employer" i]',          p.get("current_company", "")),
            ('input[placeholder*="designation" i],          input[placeholder*="current.*role" i], input[name*="designation" i]',            p.get("current_designation", "")),
            ('input[placeholder*="notice" i],               input[name*="notice" i],              input[name*="noticePeriod" i]',            p.get("notice_period", "")),
            ('input[placeholder*="current.*ctc" i],         input[placeholder*="current.*salary" i], input[name*="currentCTC" i]',          p.get("current_ctc", "")),
            ('input[placeholder*="expected.*ctc" i],        input[placeholder*="expected.*salary" i], input[name*="expectedCTC" i]',        p.get("expected_ctc", "")),

            # Education
            ('input[placeholder*="degree" i],       input[name*="degree" i],       input[placeholder*="qualification" i]', p.get("degree", "")),
            ('input[placeholder*="university" i],   input[placeholder*="college" i], input[name*="university" i]',         p.get("university", "")),
            ('input[placeholder*="graduation" i],   input[name*="graduationYear" i], input[placeholder*="passing year" i]', p.get("graduation_year", "")),

            # Skills & cover
            ('input[placeholder*="skill" i],                input[name*="skill" i]',                               p.get("skills", "")),
            ('textarea[placeholder*="skill" i],             textarea[name*="skill" i]',                            p.get("skills", "")),
            ('textarea[placeholder*="cover" i],             textarea[name*="cover" i],  textarea[name*="message" i], textarea[placeholder*="message" i]', cover_letter),
            ('textarea[placeholder*="summary" i],           textarea[name*="summary" i]',                          cover_letter),
        ]

        for sel, val in field_map:
            if not val:
                continue
            try:
                inp = page.locator(sel).first
                if await inp.count() > 0 and await inp.is_visible():
                    current = await inp.input_value()
                    if not current:
                        await inp.fill(str(val), timeout=3000)
            except Exception:
                pass

        # Handle <select> dropdowns
        try:
            selects = await page.query_selector_all("select")
            for sel_el in selects:
                try:
                    name  = ((await sel_el.get_attribute("name"))  or "").lower()
                    pid   = ((await sel_el.get_attribute("id"))     or "").lower()
                    label = name + " " + pid

                    if any(k in label for k in ["exp", "year"]):
                        try:
                            await sel_el.select_option(value=exp)
                        except Exception:
                            await sel_el.select_option(index=1)

                    elif any(k in label for k in ["notice", "joining"]):
                        try:
                            await sel_el.select_option(label="30")
                        except Exception:
                            await sel_el.select_option(index=1)

                    elif any(k in label for k in ["relocat"]):
                        try:
                            await sel_el.select_option(label="Yes")
                        except Exception:
                            await sel_el.select_option(index=1)

                    elif any(k in label for k in ["work.*mode", "wfh", "hybrid"]):
                        try:
                            await sel_el.select_option(label="Hybrid")
                        except Exception:
                            await sel_el.select_option(index=1)
                except Exception:
                    pass
        except Exception:
            pass

    async def _apply_on_company_site(self, ext_page) -> bool:
        """Apply on an external company career page with auth + CV upload."""
        try:
            await ext_page.wait_for_load_state("domcontentloaded", timeout=30000)
            await self._sleep(3)

            # Handle login / signup
            await self._handle_company_site_auth(ext_page)
            # Wait for page to stabilize after auth redirect
            try:
                await ext_page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await self._sleep(2)

            # Check for CAPTCHA after auth
            if await self._has_captcha(ext_page):
                cont = await self._show_stuck_popup(
                    ext_page,
                    "A CAPTCHA appeared. Please solve it, then click Fill & Continue."
                )
                if not cont:
                    return False

            # Click Apply / Apply Now if a landing page button exists
            for sel in [
                'button:has-text("Apply Now")', 'a:has-text("Apply Now")',
                'button:has-text("Apply for this job")', 'a:has-text("Apply for this job")',
                'button:has-text("Apply")', 'a:has-text("Apply")',
            ]:
                btn = ext_page.locator(sel).first
                if await btn.count() > 0:
                    try:
                        await btn.click()
                        await self._sleep(2)
                        break
                    except Exception:
                        pass

            # Upload CV from Naukri profile if downloaded
            if self.resume_path and Path(self.resume_path).exists():
                file_inp = ext_page.locator('input[type="file"]').first
                if await file_inp.count() > 0:
                    try:
                        await file_inp.set_input_files(self.resume_path)
                        logger.info("  CV uploaded on company site")
                        await self._sleep(2)
                    except Exception as exc:
                        logger.warning(f"  CV upload failed: {exc}")
                        cont = await self._show_stuck_popup(
                            ext_page,
                            "Could not upload CV automatically.\n\nPlease upload your resume manually, then click Fill & Continue."
                        )
                        if not cont:
                            return False

            # Fill all form fields from CV + profile
            await self._fill_company_form(ext_page)

            # Check for CAPTCHA before submit
            if await self._has_captcha(ext_page):
                cont = await self._show_stuck_popup(
                    ext_page,
                    "CAPTCHA before submission. Please solve it, then click Fill & Continue."
                )
                if not cont:
                    return False

            # Submit
            submitted = False
            for sel in [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Submit Application")', 'button:has-text("Submit")',
                'button:has-text("Apply")',
            ]:
                try:
                    btn = ext_page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click()
                        await self._sleep(3)
                        submitted = True
                        break
                except Exception:
                    pass

            if not submitted:
                cont = await self._show_stuck_popup(
                    ext_page,
                    "Could not find the Submit button.\n\nPlease submit the form manually, then click Fill & Continue."
                )
                if not cont:
                    return False

            logger.info("  [OK] Company site apply completed")
            return True

        except Exception as exc:
            logger.warning(f"  Company site apply error: {exc}")
            return False
        finally:
            try:
                await ext_page.close()
            except Exception:
                pass

    async def _answer_chatbot_question(self) -> bool:
        """Detect the current chatbot question and fill the appropriate answer."""
        answered = False

        # Get last chat bubble text (the question)
        question_text = ""
        for sel in [
            ".chatbot-question", ".bot-message", "[class*='botMsg']",
            "[class*='question']", ".chatMsg", "p[class*='question']",
        ]:
            els = await self.page.query_selector_all(sel)
            if els:
                question_text = (await els[-1].inner_text()).lower()
                break

        if not question_text:
            # Try reading all visible text in the modal as a fallback
            try:
                modal = self.page.locator("[class*='modal'], [class*='chatbot'], [class*='apply']").first
                if await modal.count() > 0:
                    question_text = (await modal.inner_text()).lower()
            except Exception:
                pass

        answer = self._resolve_answer(question_text)

        # 1. Try radio buttons / checkboxes that match the answer
        if answer.lower() in ("yes", "no"):
            radio = self.page.locator(
                f"input[type='radio'][value='{answer}'], "
                f"label:has-text('{answer}')"
            ).first
            if await radio.count() > 0:
                await radio.click()
                answered = True

        # 2. Try dropdown / select
        select_el = self.page.locator("select:visible").first
        if await select_el.count() > 0:
            try:
                await select_el.select_option(label=answer)
                answered = True
            except Exception:
                try:
                    await select_el.select_option(index=1)
                    answered = True
                except Exception:
                    pass

        # 3. Try text input
        if not answered:
            text_input = self.page.locator(
                "input[type='text']:visible, input[type='number']:visible, textarea:visible"
            ).first
            if await text_input.count() > 0:
                try:
                    await text_input.clear()
                    await text_input.fill(answer)
                    answered = True
                except Exception:
                    pass

        return answered

    def _resolve_answer(self, question_text: str) -> str:
        """Match question text to a profile value."""
        if not question_text or not self.profile:
            return "Yes"

        qt_lower = question_text.strip().lower()

        if qt_lower in self.custom_answers:
            ans = self.custom_answers[qt_lower]
            if ans and ans != "TODO":
                return str(ans)

        for pattern, key in QUESTION_MAP:
            if re.search(pattern, question_text, re.IGNORECASE):
                return str(self.profile.get(key, "Yes"))

        if qt_lower not in self.custom_answers:
            self.custom_answers[qt_lower] = "TODO"
            self._save_answers()
            logger.warning(f"New question found: '{question_text}'. Added to answers.json. Please fill it!")

        return "Yes"

    async def _is_success(self) -> bool:
        for indicator in [
            'text="Your application has been submitted"',
            'text="Applied Successfully"',
            'text="Application Submitted"',
            'text="successfully applied"',
            '[class*="success"][class*="apply"]',
            '[class*="successMsg"]',
        ]:
            try:
                if await self.page.locator(indicator).count() > 0:
                    return True
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------ #
    #  Logging helpers                                                     #
    # ------------------------------------------------------------------ #

    def _log_job(self, job: dict, status: str):
        timestamp = datetime.now().isoformat()
        self.applied_jobs[job["id"]] = {
            "title":     job["title"],
            "company":   job["company"],
            "url":       job["url"],
            "status":    status,
            "timestamp": timestamp,
        }
        self._save_log()

        # Log JD text to a separate JSONL file for behavioral analysis
        jd_entry = {
            "id": job.get("id"),
            "title": job.get("title"),
            "company": job.get("company"),
            "url": job.get("url"),
            "status": status,
            "timestamp": timestamp,
            "jd_text": job.get("jd_text", "")
        }
        try:
            with open(self.jd_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(jd_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write JD log: {e}")

    # ------------------------------------------------------------------ #
    #  Main run loop                                                       #
    # ------------------------------------------------------------------ #

    async def run(self):
        await self.start()
        try:
            if not await self.login():
                logger.error("Cannot proceed without login. Exiting.")
                return

            await self.fetch_profile()

            max_apply = self.settings.get("max_apply_per_run", 20)
            delay     = self.settings.get("delay_between_applies", 4)
            confirm   = self.settings.get("confirm_before_apply", True)
            max_pages = self.settings.get("max_pages", 3)
            applied_count = 0
            skipped_count = 0
            seen_ids = set()

            if self.search_cfg.get("custom_url"):
                keywords = ["CUSTOM_URL"]
            else:
                keywords = (
                    self.search_cfg.get("keywords")
                    or [self.search_cfg.get("keyword", "")]
                )

            should_stop = False
            for keyword in keywords:
                if should_stop or applied_count >= max_apply:
                    break
                logger.info(f"==> Keyword search: '{keyword}'")
                for page_num in range(1, max_pages + 1):
                    if should_stop or applied_count >= max_apply:
                        break
                    url = self._build_search_url(page_num, keyword=keyword)
                    logger.info(f"  Page {page_num}: {url}")
                    try:
                        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        logger.error(f"Failed to navigate to search page ({e}). Ending session early.")
                        should_stop = True
                        break

                    try:
                        await self.page.wait_for_selector(self._CARD_WAIT_SEL, timeout=12000)
                    except Exception:
                        logger.warning("  Job card wait timed out — trying anyway.")
                    await self._sleep(2)

                    page_jobs, card_count = await self._extract_jobs_from_page()
                    new_jobs  = [j for j in page_jobs if j["id"] not in seen_ids]
                    seen_ids.update(j["id"] for j in new_jobs)
                    logger.info(f"  -> {len(new_jobs)} new jobs on page {page_num}")

                    if card_count == 0:
                        logger.info("  No more jobs on this keyword — next keyword.")
                        break

                    for job in new_jobs:
                        if should_stop or applied_count >= max_apply:
                            break

                        # Browser health check — stop cleanly if window was closed
                        try:
                            await self.page.title()
                        except Exception:
                            logger.error("Browser closed — ending session early.")
                            should_stop = True
                            break

                        print(
                            f"\n{'-'*55}\n"
                            f"  Job {applied_count + skipped_count + 1} (Applied: {applied_count}/{max_apply})\n"
                            f"  Title   : {job['title']}\n"
                            f"  Company : {job['company']}\n"
                            f"  URL     : {job['url']}\n"
                            f"{'-'*55}"
                        )

                        if confirm:
                            choice = await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: input("  Apply? [y=yes / n=skip / q=quit] : ").strip().lower()
                            )
                            if choice == "q":
                                print("  Quitting session.")
                                should_stop = True
                                break
                            if choice != "y":
                                print("  Skipped.")
                                skipped_count += 1
                                self._log_job(job, "user_skipped")
                                continue

                        success = await self.apply_to_job(job)
                        if success:
                            applied_count += 1
                        else:
                            skipped_count += 1
                        await self._sleep(delay)

            logger.info(
                f"\n{'='*50}\n"
                f"Session complete.\n"
                f"  Applied  : {applied_count}\n"
                f"  Skipped  : {skipped_count}\n"
                f"  Log file : {self.log_file.resolve()}\n"
                f"{'='*50}"
            )
        finally:
            await self.stop()

    async def _sleep(self, seconds: float):
        await asyncio.sleep(seconds)
