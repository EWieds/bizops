"""A real browser for agents: sign up for services, check out with an approved card, run web dashboards.

* Headless Chromium via Playwright (optional dependency: `pip install -e '.[browser]'`; the Docker image
  has it). Each business gets its own persistent profile under data/browser/<org>, so logins and
  cookies survive between runs and businesses never share sessions.
* Each agent drives its own tab. Pages are returned as text: the URL, title, readable text and a
  numbered list of the things you can click or type into (refs like e12, or f1e3 inside an iframe,
  which is how card fields on checkout pages appear).
* SSRF guard: every page navigation is checked like fetch_url (no private or local addresses).
* Secrets: tools resolve {{card:...}} and {{login:...}} placeholders right before typing; the values
  are redacted from everything returned, so the model never sees a card number or a password.
* Everything a page says is untrusted data, wrapped like web fetches.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .engine import PolicyRefusal
from .webclient import check_url, wrap_untrusted

SNAPSHOT_JS = r"""
(prefix) => {
  document.querySelectorAll('[data-bz-ref]').forEach(e => e.removeAttribute('data-bz-ref'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],' +
              '[role=radio],[role=tab],[role=menuitem],[role=option],[role=combobox],[role=switch],[contenteditable=true]';
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const label = el => {
    const bits = [el.getAttribute('aria-label'), el.labels && el.labels[0] && el.labels[0].innerText,
                  el.getAttribute('placeholder'), el.getAttribute('title'), el.getAttribute('alt')];
    if (!['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) bits.unshift(el.innerText);
    bits.push(el.getAttribute('name'), el.getAttribute('autocomplete'));
    for (const b of bits) { const c = clean(b); if (c) return c.slice(0, 80); }
    return '';
  };
  const items = []; let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (items.length >= 250) break;
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    if (!visible(el)) continue;
    const ref = prefix + 'e' + (i++);
    el.setAttribute('data-bz-ref', ref);
    const d = {ref, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || el.getAttribute('role') || '',
               label: label(el)};
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      if (el.type === 'checkbox' || el.type === 'radio') d.checked = el.checked;
      else d.value = el.type === 'password' ? (el.value ? '(filled)' : '') : clean(el.value).slice(0, 60);
    }
    if (el.tagName === 'SELECT') {
      d.value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
      d.options = Array.from(el.options).slice(0, 20).map(o => clean(o.text));
    }
    if (el.tagName === 'A') d.href = (el.getAttribute('href') || '').slice(0, 120);
    if (el.disabled) d.disabled = true;
    items.push(d);
  }
  return {title: document.title, text: clean(document.body ? document.body.innerText : '').slice(0, 8000), items};
}
"""


def _fmt_item(d: dict) -> str:
    kind = d["tag"] + (f"[{d['type']}]" if d.get("type") else "")
    s = f"[{d['ref']}] {kind} \"{d.get('label', '')}\""
    if "value" in d:
        s += f" value=\"{d['value']}\""
    if "checked" in d:
        s += " checked" if d["checked"] else " unchecked"
    if d.get("options"):
        s += " options=" + " | ".join(d["options"])
    if d.get("href"):
        s += f" -> {d['href']}"
    if d.get("disabled"):
        s += " (disabled)"
    return s


class Browser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Any = None
        self._contexts: dict[str, Any] = {}           # org_id -> BrowserContext
        self._pages: dict[str, Any] = {}              # agent_id -> Page
        self._frames: dict[str, dict[str, Any]] = {}  # agent_id -> ref prefix -> Frame
        self._secrets: dict[str, set[str]] = {}       # agent_id -> values to redact
        self._used: dict[str, float] = {}             # agent_id -> last use
        self._lock = asyncio.Lock()

    @property
    def cfg(self) -> dict[str, Any]:
        return self.settings.policy.get("browser", {})

    @staticmethod
    def installed() -> bool:
        try:
            import playwright.async_api  # noqa: F401
            return True
        except ImportError:
            return False

    async def _context(self, org_id: str) -> Any:
        if org_id in self._contexts:
            return self._contexts[org_id]
        if not self.installed():
            raise PolicyRefusal("browser.unavailable", "the browser isn't installed on this server "
                                                       "(pip install -e '.[browser]' and playwright's Chromium)")
        from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
        profile = Path(self.settings.data_dir) / "browser" / org_id
        profile.mkdir(parents=True, exist_ok=True)
        kw: dict[str, Any] = {"headless": True, "viewport": {"width": 1280, "height": 900},
                              "args": ["--disable-dev-shm-usage", "--no-sandbox"], "locale": "en-US"}
        exe = os.environ.get("BIZOPS_CHROMIUM")
        if exe:
            kw["executable_path"] = exe
        ctx = await self._pw.chromium.launch_persistent_context(str(profile), **kw)
        ctx.set_default_timeout(float(self.cfg.get("action_timeout_s", 15)) * 1000)
        blocked = list(self.settings.policy.get("web", {}).get("blocked_domains", []))
        allow_private = bool(self.cfg.get("allow_private_hosts", False))

        async def guard(route: Any) -> None:
            req = route.request
            if req.is_navigation_request() and not allow_private:
                try:
                    await check_url(req.url, blocked)
                except PolicyRefusal:
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await ctx.route("**/*", guard)
        self._contexts[org_id] = ctx
        return ctx

    async def _page(self, agent: dict) -> Any:
        aid = agent["id"]
        page = self._pages.get(aid)
        if page is not None and not page.is_closed():
            self._used[aid] = time.monotonic()
            return page
        ctx = await self._context(agent["org_id"])
        page = await ctx.new_page()
        self._pages[aid] = page
        self._used[aid] = time.monotonic()
        return page

    async def _settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            await page.wait_for_load_state("networkidle", timeout=4_000)
        except Exception:
            pass

    def _redact(self, agent_id: str, text: str) -> str:
        for s in sorted(self._secrets.get(agent_id, ()), key=len, reverse=True):
            if len(s) >= 3:
                text = text.replace(s, "•••")
        return text

    async def snapshot(self, agent: dict) -> str:
        page = self._pages.get(agent["id"])
        if page is None or page.is_closed():
            return "No page open. Use browser_open first."
        await self._settle(page)
        frames = {"": page.main_frame}
        main = await page.main_frame.evaluate(SNAPSHOT_JS, "")
        lines = [f"URL: {page.url}", f"Title: {main['title']}", "", "--- page text ---", main["text"] or "(empty)",
                 "", "--- elements (use the ref) ---"]
        lines += [_fmt_item(d) for d in main["items"]]
        n = 0
        for fr in page.frames:
            if fr is page.main_frame or fr.is_detached():
                continue
            try:
                sub = await fr.evaluate(SNAPSHOT_JS, f"f{n + 1}")
            except Exception:
                continue
            if not sub["items"]:
                continue
            n += 1
            frames[f"f{n}"] = fr
            lines.append(f"--- inside frame f{n} ({(fr.url or '')[:80]}) ---")
            lines += [_fmt_item(d) for d in sub["items"]]
            if n >= 8:
                break
        self._frames[agent["id"]] = frames
        body = "\n".join(lines)[: int(self.cfg.get("max_chars_returned", 20000))]
        return wrap_untrusted(page.url, self._redact(agent["id"], body))

    def _locate(self, agent: dict, ref: str) -> Any:
        frames = self._frames.get(agent["id"])
        if not frames:
            raise PolicyRefusal("browser.no_snapshot", "take a browser_snapshot first; refs come from it")
        prefix = ref.split("e", 1)[0] if ref.startswith("f") else ""
        frame = frames.get(prefix)
        if frame is None or frame.is_detached():
            raise PolicyRefusal("browser.stale_ref", f"ref {ref} is stale; take a new browser_snapshot")
        return frame.locator(f'[data-bz-ref="{ref}"]').first

    async def open(self, agent: dict, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not self.cfg.get("allow_private_hosts", False):
            await check_url(url, list(self.settings.policy.get("web", {}).get("blocked_domains", [])))
        async with self._lock:
            page = await self._page(agent)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            if "blockedbyclient" in str(e).lower():
                raise PolicyRefusal("web.ssrf", "that address is not a public web page")
            raise
        return await self.snapshot(agent)

    async def click(self, agent: dict, ref: str) -> str:
        await self._locate(agent, ref).click()
        return await self.snapshot(agent)

    async def type(self, agent: dict, ref: str, text: str, *, secrets: list[str] | None = None,
                   submit: bool = False, slowly: bool = False) -> str:
        if secrets:
            self._secrets.setdefault(agent["id"], set()).update(s for s in secrets if s)
        loc = self._locate(agent, ref)
        if slowly:
            await loc.click()
            await loc.fill("")
            await loc.press_sequentially(text, delay=25)
        else:
            await loc.fill(text)
        if submit:
            await loc.press("Enter")
        return await self.snapshot(agent)

    async def select(self, agent: dict, ref: str, option: str) -> str:
        loc = self._locate(agent, ref)
        try:
            await loc.select_option(label=option)
        except Exception:
            await loc.select_option(value=option)
        return await self.snapshot(agent)

    async def press(self, agent: dict, key: str) -> str:
        page = self._pages.get(agent["id"])
        if page is None or page.is_closed():
            return "No page open. Use browser_open first."
        await page.keyboard.press(key)
        return await self.snapshot(agent)

    async def close_idle(self, max_idle_s: float = 1800) -> int:
        """Close tabs nobody used for a while (their logins stay in the profile)."""
        n = 0
        for aid, last in list(self._used.items()):
            if time.monotonic() - last > max_idle_s:
                page = self._pages.pop(aid, None)
                self._used.pop(aid, None)
                self._frames.pop(aid, None)
                self._secrets.pop(aid, None)
                if page is not None and not page.is_closed():
                    await page.close()
                    n += 1
        return n

    async def aclose(self) -> None:
        for ctx in self._contexts.values():
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        self._pages.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
