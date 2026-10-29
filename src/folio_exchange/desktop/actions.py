"""The window's setup actions (D3): connecting to FOLIO, adding a feed, a feed's settings, delivery, password and
key, recipes, renaming the site folder, the scheduled task and alerts. ``MainWindow`` takes these in as a
mixin, so ``window.py`` keeps the reading, the feeds' actions and closing.

As for the feeds' actions: whatever changes a file, reaches the network or stores a secret runs off the window's
thread, as a job through the ``Jobs`` the browser page shares (one at a time, refused at once while another runs,
here or on the page) or, for looking only, as an errand whose answer is dropped when a newer one was asked for.
Every job here has no feed (the page then shows it only as running), and the window remembers what it is about
(``job_about``). A secret (the FOLIO, SFTP or mail password, a key's passphrase, a webhook address) is taken
from its box, which is emptied at once, and handed to the job's work as an argument: it is not kept on the window
or a page, and never logged or put in the job's lines.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .. import runner, setupops, siteops, sources, views
from ..config import load_site
from ..errors import ConfigError, FxError
from . import model, places
from .alerts import AlertsDialog
from .delivery import DeliveryDialog
from .options import task_command
from .settings import SettingsDialog

log = logging.getLogger(__name__)

# What a job of each kind reports to when it ends.
SETUP_DONE = {"connect": "_connected", "add-feed": "_feed_added", "settings": "_settings_saved",
              "delivery": "_delivery_saved", "password": "_password_saved", "private-key": "_key_saved",
              "recipe-install": "_recipes_installed", "alerts": "_alerts_saved", "test-alert": "_alert_tested"}
CONFLICT = ("changed since this form was opened", "changed while this was being saved")


def _load(root: Path):
    site, library, _feeds, _errors = views.load(root)
    return site, library


def _read_key(path: str) -> str:
    """A private key file's text, read only when it is saved (it never goes anywhere else)."""
    p = Path(path)
    try:
        if p.stat().st_size > setupops.MAX_KEY_BYTES:
            raise ConfigError("That file is too large to be a private key.")
        data = p.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Couldn't read the key file ({exc.strerror or exc}).") from None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError("That file isn't a private key. The public key (ending .pub) is the one the vendor "
                          "gets.") from None


class SetupActions:
    """Mixed into MainWindow (see window.py for the attributes it uses)."""

    # ------------------------------------------------------------ plumbing
    def _setup_job(self, kind: str, about: object, work, refused) -> bool:
        """Start a setup job (no feed: the page shows it only as running); ``about`` says what it is for."""
        job = self._start(kind, None, work, refused)
        if job is None:
            return False
        self.job_about = about
        return True

    def _setup_done(self, kind: str, state: dict) -> None:
        about, self.job_about = self.job_about, None
        getattr(self, SETUP_DONE[kind])(state, about)
        self.refresh()

    @staticmethod
    def _failed(state: dict) -> str | None:
        return None if state.get("status") == "done" else str(state.get("error") or "No reason was given.")

    def _setup_errand(self, key: tuple, result, message: str | None) -> None:
        what = key[0]
        n = key[-1]
        if what == "recipes":
            if self.asked.get(("recipes", key[1])) != n:
                return
            if key[1] == "wizard":
                self.wizard.show_recipes(result, message)
            else:
                self.recipes.show_overview(result, message)
        elif what == "choices":
            if self.asked.get(("choices",)) != n:
                return
            self.choices = {"error": message} if message else {"data": result}
            self.wizard.show_choices(self.choices)
        elif what == "permissions":
            if self.asked.get(("permissions",)) == n:
                self.wizard.show_permissions(key[1], result, message)
        elif what == "feed-settings":
            self._feed_settings_read(key, result, message)
        elif what == "recipe-check":
            if self.asked.get(("recipe-check", key[1])) == n:
                self.recipes.checked(key[1], result, message)
        elif what == "rename":
            self._renamed(result, message)
        elif what == "register":
            self._registered(result, message)
        elif what == "task":
            if self.asked.get(("task",)) == n:
                self.options.show_registered(result, message)
        elif what == "alert-settings":
            if self.asked.get(("alert-settings", key[1])) != n:
                return
            if key[1] == "dialog":
                self._open_alerts(result, message)
            else:
                self.options.show_alerts(result, message)

    # ------------------------------------------------------------ connecting to FOLIO
    def connect_folio(self, form) -> None:
        """Log in with what ``form`` holds; only if FOLIO accepts it is the password stored and site.yaml written."""
        values = form.values()
        password = form.take_password()                 # the box is empty from here on
        self._follow_page()
        root = self.site_root
        about = "setup" if form is self.setup.form else "change"

        def work(v=values, typed=password) -> dict:
            log.info("Logging in to FOLIO at %s as %s (tenant %s)", v["base_url"], v["username"], v["tenant"])
            return setupops.connect(root, v["base_url"], v["tenant"], v["username"], typed, via="app")

        if self._setup_job("connect", about, work, form.failed):
            form.working(True)

    def _connected(self, state: dict, about: str) -> None:
        form = self.setup.form if about == "setup" else self.connect_form
        form.working(False)
        problem = self._failed(state)
        if problem:
            form.failed(problem)
            if about == "change" and form.isHidden():       # the person left the view while FOLIO answered
                self.connection.say(f"The connection wasn't changed. {problem}")
            return
        self._forget_choices()
        res = state.get("result") or {}
        if about == "setup":
            self.wizard.say("Connected to FOLIO. Next, add a vendor feed.")
            self.pending_select = ("add", None)
            return
        self.connection.close_form()
        where = model.password_words((res.get("folio") or {}).get("password"))
        self.connection.say(f"Logged in and saved. Password: {where}." + (f" {res['notice']}" if res.get("notice")
                                                                          else ""))

    def _forget_choices(self) -> None:
        """The connection changed: the tenant's lists read before may be another tenant's. They are read again when
        next needed, and a reading of them under way (for the wizard, or for a settings form opening) is dropped
        or asked again."""
        self.choices = None
        self._ask(("choices",))
        self.wizard.forget_choices()
        if self.opening_settings is not None:
            self._read_feed_settings("settings", self.opening_settings, with_choices=True)

    def open_connect_form(self) -> None:
        state = (self.snap or {}).get("state") or {}
        self.connection.open_form(state.get("folio") or {}, bool(state.get("keyring")))

    # ------------------------------------------------------------ the new-feed wizard
    def read_recipes(self, where: str) -> None:
        self._follow_page()
        root = self.site_root
        n = self._ask(("recipes", where))
        self.errands.run(("recipes", where, n), lambda: views.recipes_overview(root))

    def want_permissions(self, uses: str) -> None:
        self._follow_page()
        root = self.site_root
        n = self._ask(("permissions",))

        def look() -> dict:
            site, library = _load(root)
            return setupops.recipe_permissions(site, library, uses)

        self.errands.run(("permissions", uses, n), look)

    def want_choices(self, force: bool) -> None:
        """The tenant's locations, item statuses and note types, read once while the window is open."""
        if self.choices is not None and not force and "data" in self.choices:
            self.wizard.show_choices(self.choices)
            return
        self._follow_page()
        root = self.site_root
        n = self._ask(("choices",))
        self.errands.run(("choices", n), lambda: setupops.tenant_choices(load_site(root)))

    def add_feed(self) -> None:
        req = self.wizard.request()
        self._follow_page()
        root = self.site_root

        def work() -> dict:
            log.info("Adding the feed %s on %s (paused, with nowhere to send files)", req["name"], req["uses"])
            site, library = _load(root)
            return setupops.add_feed(site, library, req["name"], req["uses"], req["values"], via="app")

        if self._setup_job("add-feed", req["name"], work, self.wizard.failed):
            self.wizard.adding(True)

    def _feed_added(self, state: dict, name: str) -> None:
        problem = self._failed(state)
        if problem:
            self.wizard.failed(problem)
            return
        res = state.get("result") or {}
        self.wizard.reset()
        self.wizard.recipes = None                         # "used by" has changed
        self.pending_select = (f"feed:{name}", res.get("title") or name)

    def cancel_wizard(self) -> None:
        self.wizard.reset()
        feeds = ((self.snap or {}).get("summary") or {}).get("feeds") or []
        self.select(f"feed:{feeds[0]['name']}" if feeds else "home")

    # ------------------------------------------------------------ a feed's settings and delivery
    def _read_feed_settings(self, purpose: str, name: str, with_choices: bool = False) -> None:
        self._follow_page()
        root = self.site_root
        n = self._ask(("feed-settings", purpose))
        cached = self.choices if self.choices and "data" in self.choices else None

        def look() -> dict:
            site, library = _load(root)
            out = {"settings": setupops.feed_settings(site, library, name), "choices": cached}
            if with_choices and cached is None:
                try:
                    out["choices"] = {"data": setupops.tenant_choices(site)}
                except Exception as exc:  # noqa: BLE001 - without the lists, codes are typed in
                    out["choices"] = {"error": str(exc)}
            return out

        self.errands.run(("feed-settings", purpose, name, n), look)

    def edit_settings(self, name: str) -> None:
        if self.settings_dialog is not None:
            self.settings_dialog.raise_()
            return
        self.feed.say("Opening the settings…")
        self.opening_settings = name
        self._read_feed_settings("settings", name, with_choices=True)

    def change_delivery(self, name: str) -> None:
        if self.delivery_dialog is not None:
            self.delivery_dialog.raise_()
            return
        self._read_feed_settings("delivery", name)

    def _feed_settings_read(self, key: tuple, result, message: str | None) -> None:
        _what, purpose, name, n = key
        if self.asked.get(("feed-settings", purpose)) != n:
            return
        if purpose == "settings":
            self.opening_settings = None
            self.feed.say(message)
            if message or self.settings_dialog is not None or self.quitting:
                return
            if result.get("choices") and "data" in result["choices"]:
                self.choices = result["choices"]
            dialog = self.settings_dialog = SettingsDialog(name, result["settings"], result.get("choices"), self)
            dialog.save.connect(self.save_settings)
            dialog.reapply.connect(self.reapply_settings)
            dialog.finished.connect(self._settings_closed)
            dialog.open()
        elif purpose == "reapply":
            dialog = self.settings_dialog
            if dialog is not None:
                if message:
                    dialog.failed(message)
                else:
                    dialog.working(False)                  # the form was not editable while the file was read
                    dialog.rebase(result["settings"], overlay=dialog.send)
                    dialog.show_review()
        elif purpose == "delivery":
            if message:
                self.feed.say(message)
                return
            if self.delivery_dialog is not None or self.quitting:
                return
            dialog = self.delivery_dialog = DeliveryDialog(name, result["settings"], self)
            dialog.save.connect(self.save_delivery)
            dialog.refresh_version.connect(lambda: self._read_feed_settings("delivery-again", name))
            dialog.finished.connect(self._delivery_closed)
            dialog.open()
        elif purpose == "delivery-again":
            dialog = self.delivery_dialog
            if dialog is not None:
                if message:
                    dialog.failed(message)
                else:
                    dialog.rebase(result["settings"], keep_form=True)
                    dialog._save()

    def _settings_closed(self, _code: int) -> None:
        dialog, self.settings_dialog = self.settings_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def _delivery_closed(self, _code: int) -> None:
        dialog, self.delivery_dialog = self.delivery_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def save_settings(self, values: dict, version: str) -> None:
        dialog = self.settings_dialog
        if dialog is None:
            return
        name = dialog.name
        self._follow_page()
        root = self.site_root

        def work() -> dict:
            log.info("Saving the settings of %s: %s", name, ", ".join(values) or "nothing changed")
            site, library = _load(root)
            return setupops.save_feed(site, library, name, values, version=version, via="app")

        if self._setup_job("settings", name, work, dialog.failed):
            dialog.working(True)

    def reapply_settings(self, _values: dict) -> None:
        if self.settings_dialog is not None:
            self._read_feed_settings("reapply", self.settings_dialog.name)

    def _settings_saved(self, state: dict, name: str) -> None:
        dialog = self.settings_dialog
        problem = self._failed(state)
        if dialog is None or dialog.name != name:
            self.feed.say(f"Settings for {name} weren't saved: {problem}" if problem else
                          f"Settings for {name} saved.")
            return
        if problem:
            (dialog.conflict if any(c in problem for c in CONFLICT) else dialog.failed)(problem)
            return
        dialog.working(False)
        dialog.accept()
        changed = (state.get("result") or {}).get("changed")
        self.feed.say("Settings saved. Use Preview to see the effect." if changed else "Nothing needed saving.")

    def save_delivery(self, form: dict, version: str) -> None:
        dialog = self.delivery_dialog
        if dialog is None:
            return
        name = dialog.name
        self._follow_page()
        root = self.site_root

        def work() -> dict:
            where = f"{form.get('host')}:{form.get('port')}" if form.get("transport") == "sftp" else form.get(
                "directory") or "nowhere"
            log.info("Saving the delivery of %s: %s, %s", name, form.get("transport"), where)
            site, library = _load(root)
            return setupops.save_delivery(site, library, name, form, version=version, via="app")

        if self._setup_job("delivery", name, work, dialog.failed):
            dialog.working(True)

    def _delivery_saved(self, state: dict, name: str) -> None:
        dialog = self.delivery_dialog
        problem = self._failed(state)
        if problem:
            if dialog is not None and dialog.name == name:
                (dialog.conflict if any(c in problem for c in CONFLICT) else dialog.failed)(problem)
            else:
                self.feed.say(f"Delivery for {name} wasn't saved: {problem}")
            return
        res = dict(state.get("result") or {})
        server_key = res.pop("server_key", None)
        sd = res.get("delivery") or {}
        words = ("Delivery saved. The server key isn't confirmed yet, so the feed is paused and nothing logs in."
                 if sd.get("transport") == "sftp" and not sd.get("host_key_confirmed") else
                 "Saved. The feed is a dry run." if sd.get("transport") == "none" else
                 "Delivery saved. Use Test delivery to try it.")
        if dialog is not None and dialog.name == name:
            dialog.working(False)
            dialog.accept()
        self.feed.delivery_saved(name, server_key, words)

    def save_password(self, name: str) -> None:
        password = self.feed.login.take_password()       # the box is empty from here on
        self._follow_page()
        root = self.site_root

        def work(typed=password) -> dict:
            log.info("Storing the SFTP password of %s in Credential Manager", name)
            site, library = _load(root)
            return setupops.store_sftp_password(site, library, name, typed, via="app")

        if self._setup_job("password", name, work, self.feed.login.failed):
            self.feed.login.working(True)

    def _password_saved(self, state: dict, name: str) -> None:
        """The login box shows the answer only while it shows the same feed; else the feed page's line says it,
        naming the feed (there is one box, and the person may have moved to another feed meanwhile)."""
        problem = self._failed(state)
        self.feed.login.working(False)
        if problem and name == self.feed.name:
            self.feed.login.failed(problem)
        elif problem:
            self.feed.say(f"SFTP password for {name} wasn't saved: {problem}")
        else:
            self.feed.say(f"Password saved in Credential Manager. {name} logs in with it from now on.")

    def save_key(self, name: str) -> None:
        path, passphrase = self.feed.login.take_key()     # both boxes are empty from here on
        self._follow_page()
        root = self.site_root

        def work(chosen=path, phrase=passphrase) -> dict:
            log.info("Storing an SFTP private key for %s", name)
            key_text = _read_key(chosen)
            site, library = _load(root)
            return setupops.store_private_key(site, library, name, key_text, phrase, via="app")

        if self._setup_job("private-key", name, work, lambda m: self.feed.login.failed(m, key=True)):
            self.feed.login.working(True)

    def _key_saved(self, state: dict, name: str) -> None:
        """As ``_password_saved``: a key's fingerprint is never shown on another feed's Delivery tab."""
        problem = self._failed(state)
        self.feed.login.working(False)
        here = name == self.feed.name
        if problem:
            if here:
                self.feed.login.failed(problem, key=True)
            else:
                self.feed.say(f"SFTP key for {name} wasn't saved: {problem}")
            return
        public = (state.get("result") or {}).get("key") or ""
        if here:
            self.feed.login.saved_key(public)
            self.feed.say(f"Key saved. {name} logs in with it from now on.")
        else:
            self.feed.say(f"Key saved for {name}, which logs in with it from now on. Its public fingerprint is "
                          f"{public}.")

    # ------------------------------------------------------------ recipes
    def check_source(self, sid: str) -> None:
        self._follow_page()
        root = self.site_root
        n = self._ask(("recipe-check", sid))

        def look() -> dict:
            site, library = _load(root)
            return sources.check(site, library, sid)

        self.errands.run(("recipe-check", sid, n), look)

    def install_recipes(self, sid: str, commit: str) -> None:
        self._follow_page()
        root = self.site_root

        def work() -> dict:
            log.info("Installing the new recipe versions of %s at commit %s", sid, commit[:7])
            site, library = _load(root)
            return sources.install(site, library, sid, commit, via="app")

        if self._setup_job("recipe-install", sid, work, lambda m: self.recipes.install_refused(sid, m)):
            self._ask(("recipe-check", sid))               # a check's answer from before would undo what follows
            self.recipes.installing(sid, True)

    def _recipes_installed(self, state: dict, sid: str) -> None:
        self._ask(("recipe-check", sid))
        problem = self._failed(state)
        if problem:
            self.recipes.install_refused(sid, problem)
            return
        self.recipes.installed_now(sid, state.get("result") or {})
        self.wizard.recipes = None
        self.read_recipes("page")

    # ------------------------------------------------------------ options: the site folder
    def rename_site(self, name: str) -> None:
        """Rename the site folder (same parent) as the running action, as the page does; refused at once while
        another runs. It waits for a reading of the site to end first, so no file of it is open."""
        job = self.jobs.running()
        if job is not None:
            self.options.rename_failed(f"Wait for the {model.job_words(job.kind, job.feed)} to finish, then rename "
                                       "the folder.")
            return
        if self.toggling or self.reviewing:
            # Not jobs, but each uses the folder: a pause or resume rewrites a feed file and then the activity log,
            # a release question reads what would be sent. Neither may be cut off by the move.
            self.options.rename_failed("Wait for the pause or resume to be saved, then rename the folder."
                                       if self.toggling else
                                       "Finish with Review and send… first, then rename the folder.")
            return
        if self.renaming:
            return
        self.renaming = name
        self.options.renaming(True)
        self.sync_busy()                                   # nothing else starts while it waits for the reading
        if not self.reader.busy:
            self._start_rename()

    def _start_rename(self) -> None:
        name, self.renaming_started = self.renaming, True
        self._follow_page()
        root, jobs = self.site_root, self.jobs

        def refuse(job) -> str:
            return f"Wait for the {model.job_words(job.kind, job.feed)} to finish, then rename the folder."

        def work() -> dict:
            try:
                with jobs.holding("rename", refuse):       # the running action: no other starts meanwhile
                    return siteops.rename_site(root, name, via="app")
            except Exception as exc:
                # A step after the move itself (reading the site again, the alert secrets, the activity log)
                # failed: the folder has moved all the same, so the window must follow it, and say what went wrong.
                new = root.parent / name
                if root.exists() or not new.is_dir():
                    raise
                why = str(exc) if isinstance(exc, FxError) else f"{type(exc).__name__}: {exc}"
                log.warning("The site folder moved to %s, but what follows the move failed: %s", new, why)
                return {"root": str(new.resolve()), "name": name, "notes": [], "failed": why}

        self.errands.run(("rename", self._ask(("rename",))), work)
        self.sync_busy()

    def _renamed(self, res: dict | None, message: str | None) -> None:
        self.renaming = None
        self.renaming_started = False
        if message:
            self.options.rename_failed(message)
        else:
            self._moved_to(Path(res["root"]))
            self.options.renamed(res)
        if self.closing_after_job and self.jobs.running() is None and not self.toggling:
            self.close_now()                               # it was waited for, and the window has followed the folder
            return
        self.sync_busy()
        self.refresh()
        if not message:
            self.look_for_task()                           # the task is named after the folder: ask again

    def _moved_to(self, new: Path) -> None:
        """This window renamed the site folder: carry on with the new one (as after a rename on the page)."""
        old, self.site_root = self.site_root, new
        self.page.follow(new)
        log.info("The site folder is now %s (renamed from %s)", new, old)
        places.remember_site(new, replaces=old)
        if self.instance is not None and not self.instance.rename(places.instance_name(new)):
            log.warning("Another window already uses the renamed folder %s", new)
        self._set_title(new.name)

    # ------------------------------------------------------------ options: the scheduled task
    def look_for_task(self) -> None:
        if not self.options.windows:
            return
        self._follow_page()
        root = self.site_root
        n = self._ask(("task",))
        self.options.show_registered(None)
        self.errands.run(("task", n), lambda: siteops.task_registered(root))

    def register_task(self) -> None:
        """Start `folio-exchange schedule --register` in a console window of its own, where schtasks asks for the
        Windows password; when that window closes, look again whether the task is there."""
        if self.registering or not self.options.windows:
            return
        self._follow_page()
        exe, args, folder = task_command(self.site_root)
        start = self.start_console
        self.registering = True
        self.options.registering(True, "A console window is open. Type your Windows password there when it asks. "
                                       "The result shows here when that window closes.")
        log.info("Registering the scheduled task for %s in a console window", self.site_root)
        self.errands.run(("register", self._ask(("register",))), lambda: start(exe, args, folder))

    def _registered(self, code: int | None, message: str | None) -> None:
        self.registering = False
        if message:
            words = f"Couldn't start the console window: {message}"
        elif code == 0:
            words = "Registered. Task Scheduler starts the runs every hour from now on."
        else:
            words = (f"The task wasn't registered (exit code {code}). If you mistyped the Windows password, try "
                     "again.")
        self.options.registering(False, words)
        self.look_for_task()

    # ------------------------------------------------------------ options: alerts
    def read_alerts(self, purpose: str = "page") -> None:
        self._follow_page()
        root = self.site_root
        n = self._ask(("alert-settings", purpose))           # the page's and the dialog's are answered apart
        self.errands.run(("alert-settings", purpose, n), lambda: setupops.alert_settings(load_site(root)))

    def change_alerts(self) -> None:
        if self.alerts_dialog is not None:
            self.alerts_dialog.raise_()
            return
        self.read_alerts("dialog")

    def _open_alerts(self, settings: dict | None, message: str | None) -> None:
        if message:
            self.options.show_alerts(None, message)
            return
        self.options.show_alerts(settings)
        if self.alerts_dialog is not None or self.quitting:
            return
        dialog = self.alerts_dialog = AlertsDialog(settings, self)
        dialog.save.connect(self.save_alerts)
        dialog.finished.connect(self._alerts_closed)
        dialog.open()

    def _alerts_closed(self, _code: int) -> None:
        dialog, self.alerts_dialog = self.alerts_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def save_alerts(self, form: dict, version: str) -> None:
        dialog = self.alerts_dialog
        if dialog is None:
            return
        password, url = dialog.take_secrets()             # both boxes are empty from here on
        self._follow_page()
        root = self.site_root

        def work(typed=password, address=url) -> dict:
            log.info("Saving the alert settings of %s", root.name)
            return setupops.save_alerts(root, form, smtp_password=typed, webhook_url=address, version=version,
                                        via="app")

        if self._setup_job("alerts", None, work, dialog.failed):
            dialog.working(True)

    def _alerts_saved(self, state: dict, _about: None) -> None:
        dialog = self.alerts_dialog
        problem = self._failed(state)
        if problem:
            if dialog is not None:
                dialog.failed(problem)
            else:
                self.options.alert_said(f"Alert settings weren't saved: {problem}")
            return
        res = dict(state.get("result") or {})
        if dialog is not None:
            dialog.working(False)
            dialog.accept()
        self.options.show_alerts(res)
        self.options.alert_said("Alert settings saved. Send a test alert to try them." if res.get("changed") else
                                "Nothing needed saving.")

    def test_alert(self) -> None:
        self._follow_page()
        root = self.site_root

        def work() -> list[str]:
            log.info("Sending a test alert for %s", root.name)
            return runner.alert(load_site(root), None, "test", f"Test alert from the FOLIO-Exchange app for "
                                f"{root.name}.", force=True)

        if self._setup_job("test-alert", None, work, lambda m: self.options.testing(False, m)):
            self.options.testing(True)

    def _alert_tested(self, state: dict, _about: None) -> None:
        problem = self._failed(state)
        sent = (state.get("result") or []) if not problem else []
        words = (f"Couldn't send the test alert: {problem}" if problem else
                 f"Sent by {' and '.join(sent)}. Check that it arrived." if sent else
                 "The test alert wasn't sent. No alert channel is set up, or none accepted it. "
                 f"{os.path.join('logs', 'folio-exchange.log')} says why.")
        self.options.testing(False, words)
