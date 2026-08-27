import re
import json
import sys
import time
import asyncio
from pathlib import Path
from pyquotex.config import update_session
from pyquotex.http.navigator import Browser


class Login(Browser):
    """Class for Quotex login resource."""

    url = ""
    cookies = None
    ssid = None
    base_url = 'qxbroker.com'
    https_base_url = f'https://{base_url}'

    def __init__(self, api, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api = api
        self.html = None
        self.headers = self.get_headers()
        self.full_url = f"{self.https_base_url}/{api.lang}"

    def get_token(self):
        self.headers["Connection"] = "keep-alive"
        self.headers["Accept-Encoding"] = "gzip, deflate, br"
        self.headers["Accept-Language"] = "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3"
        self.headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        )
        self.headers["Referer"] = f"{self.full_url}/sign-in"
        self.headers["Upgrade-Insecure-Requests"] = "1"
        self.headers["Sec-Ch-Ua-Mobile"] = "?0"
        self.headers["Sec-Ch-Ua-Platform"] = '"Linux"'
        self.headers["Sec-Fetch-Site"] = "same-origin"
        self.headers["Sec-Fetch-User"] = "?1"
        self.headers["Sec-Fetch-Dest"] = "document"
        self.headers["Sec-Fetch-Mode"] = "navigate"
        self.headers["Dnt"] = "1"
        self.send_request(
            "GET",
            f"{self.full_url}/sign-in/modal/"
        )
        html = self.get_soup()
        match = html.find(
            "input", {"name": "_token"}
        )
        token = None if not match else match.get("value")
        return token

    async def awaiting_pin(self, data, input_message):
        self.headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.headers["Referer"] = f"{self.full_url}/sign-in/modal"
        data["keep_code"] = 1
        if not sys.stdin or not sys.stdin.isatty():
            # running under systemd: input() would either block the event loop
            # on a tty or raise EOFError on /dev/null — fail loudly instead
            raise RuntimeError(
                "Quotex asked for an e-mail PIN but there is no interactive "
                "console. Run `python bot.py` manually once to enter the code, "
                "or refresh session.json.")
        try:
            code = input(input_message)
            if not code.isdigit():
                print("Please enter a valid code.")
                await self.awaiting_pin(data, input_message)
            data["code"] = code
        except KeyboardInterrupt:
            print("\nClosing program.")
            sys.exit()

        await asyncio.sleep(1)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self.send_request(
                method="POST",
                url=f"{self.full_url}/sign-in/modal",
                data=data,
            ),
        )

    def _extract_settings(self):
        """Extract the window.settings JSON from the current response (robust)."""
        if self.response is None:
            return None
        html = getattr(self.response, "text", None)
        if not isinstance(html, str):
            try:
                html = self.response.content.decode("utf-8", errors="replace")
            except Exception:
                return None
        raw = None
        m = re.search(r"window\.settings\s*=\s*(\{.*?\})\s*;", html, re.DOTALL)
        if m:
            raw = m.group(1)
        else:
            try:
                soup = self.get_soup()
                for script in soup.find_all("script"):
                    text = script.get_text() or ""
                    if "window.settings" in text:
                        m2 = re.search(r"window\.settings\s*=\s*(\{.*?\})\s*;", text, re.DOTALL)
                        raw = m2.group(1) if m2 else re.sub(
                            "window.settings = ", "",
                            text.strip().replace(";", "")
                        )
                        break
            except Exception:
                pass
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        m3 = re.search(r'"token"\s*:\s*"([A-Za-z0-9]{16,})"', html)
        if m3:
            return {"token": m3.group(1)}
        return None

    def get_profile(self):
        # the login POST usually already redirected to /trade — try that response first
        settings_json = None
        self.response = getattr(self, "response", None)
        if self.response is not None and "trade" in str(getattr(self.response, "url", "")):
            settings_json = self._extract_settings()

        for attempt in range(3):
            if settings_json and settings_json.get("token"):
                break
            if attempt:
                time.sleep(2)
            self.response = self.send_request(
                method="GET",
                url=f"{self.full_url}/trade"
            )
            settings_json = self._extract_settings()

        if not settings_json or not settings_json.get("token"):
            try:
                if self.response is not None:
                    raw_html = getattr(self.response, "text", None)
                    if not isinstance(raw_html, str):
                        raw_html = self.response.content.decode("utf-8", errors="replace")
                    Path("login_debug.html").write_text(
                        raw_html, encoding="utf-8", errors="replace"
                    )
                    print("\u26a0\ufe0f Token extraction failed \u2014 trade page HTML "
                          "saved to login_debug.html for debugging.")
            except Exception:
                pass
            return None, None

        self.cookies = self.get_cookies()
        self.ssid = settings_json.get("token")
        self.api.session_data["cookies"] = self.cookies
        self.api.session_data["token"] = self.ssid
        self.api.session_data["user_agent"] = self.headers["User-Agent"]

        update_session(self.api.username, self.api.session_data)
        return self.response, settings_json

    def _get(self):
        return self.send_request(
            method="GET",
            url=f"{self.full_url}/trade"
        )

    async def _post(self, data):
        """Send get request for Quotex API login http resource.
        :returns: The instance of: class:`requests.Response`.
        """
        loop = asyncio.get_running_loop()
        self.response = await loop.run_in_executor(
            None,
            lambda: self.send_request(
                method="POST",
                url=f"{self.full_url}/sign-in/",
                data=data,
            ),
        )
        required_keep_code = self.get_soup().find(
            "input", {"name": "keep_code"}
        )
        if required_keep_code:
            auth_body = self.get_soup().find(
                "main", {"class": "auth__body"}
            )
            input_message = (
                f'{auth_body.find("p").text}: ' if auth_body.find("p")
                else "Insira o código PIN que acabamos "
                     "de enviar para o seu e-mail: "
            )
            await self.awaiting_pin(data, input_message)
        await asyncio.sleep(1)
        success = self.success_login()
        return success
    
    def success_login(self):
        if "trade" in self.response.url:
            return True, "Login successful."

        soup = self.get_soup()

        not_available = soup.select_one("#tab-1 > div > div.modal-sign__not-avalible__title")
        if not_available:
            return False, f"Service unavailable: {not_available.get_text(strip=True)}"

        error = soup.select_one("#tab-1 form > div:nth-child(2) > div")
        msg = error.get_text(strip=True) if error else "Unknown error"

        return False, f"Login failed. {msg}"

    async def __call__(self, username, password, user_data_dir=None):
        """Method to get Quotex API login http request.
        :param str username: The username of a Quotex server.
        :param str password: The password of a Quotex server.
        :param str user_data_dir: The optional value for path userdata.
        :returns: The instance of: class:`requests.Response`.
        """
        loop = asyncio.get_running_loop()
        # get_token() / get_profile() do blocking `requests` I/O. Running them
        # directly on the event loop is what froze the whole bot (Telegram
        # polling included) whenever Quotex was slow to answer.
        data = {
            "_token": await loop.run_in_executor(None, self.get_token),
            "email": username,
            "password": password,
            "remember": 1,
        }
        status, msg = await self._post(data)
        if status:
            _, settings_json = await loop.run_in_executor(None, self.get_profile)
            if not settings_json or not self.ssid:
                return False, (
                    "Login succeeded but session token could not be extracted "
                    "from the trade page. Page HTML saved to login_debug.html "
                    "\u2014 please share that file. Will retry."
                )

        return status, msg
