import base64
import json
import os
import sys
import time

from test_core import FAKE, Base, accounts, core


def id_token(email: str, plan: str) -> str:
    claims = {"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class AccountsBase(Base):
    def setUp(self):
        super().setUp()
        self.sign_in(self.home, "me@example.com", "plus")

    def sign_in(self, home, email="alt@example.com", plan="pro"):
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": id_token(email, plan)}}))
        return home

    def add_account(self, name, **kw):
        return self.sign_in(self.accounts / name, **kw)

    def set_state(self, home, used=(10, 5), credits=0):
        (home / "fake_state.json").write_text(json.dumps({"used": list(used), "credits": credits}))

    def state(self, home):
        return json.loads((home / "fake_state.json").read_text())

    def homes_run(self):
        return [call["codex_home"] for call in self.calls()]

    def consumes(self):
        return [json.loads(p.read_text()) for p in self.logs.glob("consume-*.json")]

    def usage(self, account=None):
        return accounts.usage_report(FAKE, account, default_override=str(self.home))


class FailoverTest(AccountsBase):
    def test_limited_default_account_fails_over_to_an_added_account(self):
        alt = self.add_account("alt")
        self.set_state(self.home, used=(100, 40))
        out = self.root / "o.png"
        result = self.gen(out)
        self.assertEqual(result["account"], "alt")
        self.assertTrue(out.exists())
        self.assertIn(str(alt), self.homes_run())

    def test_unlimited_default_account_is_used_first(self):
        self.add_account("alt")
        result = self.gen(self.root / "o.png")
        self.assertEqual(result["account"], "default")
        self.assertEqual(len(self.calls()), 1)

    def test_an_account_that_hit_its_limit_is_skipped_on_the_next_job(self):
        alt = self.add_account("alt")
        self.set_state(self.home, used=(100, 40))
        self.gen(self.root / "a.png")
        before = len(self.calls())
        self.gen(self.root / "b.png")
        self.assertEqual(len(self.calls()), before + 1)
        self.assertEqual(self.homes_run().count(str(alt)), 2)

    def test_every_account_limited_says_how_to_recover(self):
        alt = self.add_account("alt")
        self.set_state(self.home, used=(100, 40))
        self.set_state(alt, used=(30, 100))
        with self.assertRaises(core.UsageLimitError) as ctx:
            self.gen(self.root / "o.png")
        message = str(ctx.exception)
        for hint in ("every signed-in account", "account_usage", "use_rate_limit_reset",
                     "start_device_login"):
            self.assertIn(hint, message)
        self.assertFalse((self.root / "o.png").exists())

    def test_an_explicit_account_is_used_alone_without_failover(self):
        self.add_account("alt")
        self.set_state(self.home, used=(100, 40))
        with self.assertRaisesRegex(core.UsageLimitError, "account 'default'"):
            self.gen(self.root / "o.png", account="default")
        self.assertEqual(len(self.calls()), 1)

    def test_an_explicit_added_account_runs_in_its_own_home(self):
        alt = self.add_account("alt")
        result = self.gen(self.root / "o.png", account="alt")
        self.assertEqual(result["account"], "alt")
        self.assertEqual(self.homes_run(), [str(alt)])

    def test_unknown_account_is_rejected_before_running_codex(self):
        with self.assertRaisesRegex(core.CodexImageError, "no signed-in account named 'ghost'"):
            self.gen(self.root / "o.png", account="ghost")
        self.assertEqual(self.calls(), [])

    def test_edit_reports_and_honours_the_account(self):
        from PIL import Image
        src = self.root / "src.png"
        Image.new("RGBA", (48, 32), (255, 0, 255, 255)).save(src)
        self.add_account("alt")
        result = core.edit(str(src), "add a hat", str(self.root / "e.png"), box=[0, 0, 8, 8],
                           codex_bin=FAKE, codex_home=str(self.home), account="alt")
        self.assertEqual(result["account"], "alt")


class UsageTest(AccountsBase):
    def test_reports_windows_limit_and_reset_credits_for_every_account(self):
        alt = self.add_account("alt", email="alt@example.com", plan="pro")
        self.set_state(self.home, used=(100, 40), credits=2)
        self.set_state(alt, used=(20, 10))
        report = self.usage()
        default, other = report["accounts"]
        self.assertEqual(default["account"], "default")
        self.assertEqual(default["email"], "me@example.com")
        self.assertTrue(default["limit_reached"])
        self.assertEqual(default["five_hour"]["used_percent"], 100)
        self.assertEqual(default["weekly"]["window_minutes"], 10080)
        self.assertIsNotNone(default["available_again_at"])
        self.assertEqual(default["reset_credits"]["available"], 2)
        self.assertEqual(default["reset_credits"]["credits"][0]["id"], "credit-0")
        self.assertEqual((other["account"], other["limit_reached"]), ("alt", False))
        self.assertEqual(report["usable_accounts"], ["alt"])
        self.assertEqual(report["total_reset_credits"], 2)

    def test_reading_usage_marks_and_clears_limited_accounts(self):
        self.set_state(self.home, used=(100, 40))
        self.usage()
        self.assertTrue(accounts.is_exhausted("default"))
        self.set_state(self.home, used=(10, 40))
        self.usage()
        self.assertFalse(accounts.is_exhausted("default"))

    def test_a_signed_out_account_reports_an_error_without_failing_the_report(self):
        (self.accounts / "half").mkdir(parents=True)
        report = self.usage(account="half")
        self.assertIn("not signed in", report["accounts"][0]["error"])


class ResetTest(AccountsBase):
    def reset(self, account="default", **kw):
        return accounts.use_reset_credit(account, FAKE, default_override=str(self.home), **kw)

    def test_redeems_a_credit_when_the_limit_is_reached(self):
        self.set_state(self.home, used=(100, 40), credits=2)
        accounts.mark_exhausted("default")
        result = self.reset()
        self.assertEqual(result["outcome"], "reset")
        self.assertFalse(result["usage"]["limit_reached"])
        self.assertEqual(result["usage"]["reset_credits"]["available"], 1)
        self.assertFalse(accounts.is_exhausted("default"))
        [consume] = self.consumes()
        self.assertTrue(consume["consume"]["idempotencyKey"])
        self.assertEqual(consume["consume"]["creditId"], "credit-0")  # expires first
        self.assertEqual(result["credit_id"], "credit-0")
        self.assertEqual(self.gen(self.root / "o.png")["account"], "default")

    def test_a_specific_credit_can_be_chosen(self):
        self.set_state(self.home, used=(100, 40), credits=2)
        self.reset(credit_id="credit-1")
        self.assertEqual(self.consumes()[0]["consume"]["creditId"], "credit-1")

    def test_refuses_to_spend_a_credit_while_usage_is_left(self):
        self.set_state(self.home, used=(95, 40), credits=2)
        with self.assertRaisesRegex(accounts.AccountError, "has not reached its usage limit"):
            self.reset()
        self.assertEqual(self.consumes(), [])
        self.assertEqual(self.state(self.home)["credits"], 2)

    def test_force_sends_the_request_even_with_usage_left(self):
        self.set_state(self.home, used=(95, 40), credits=2)
        result = self.reset(force=True)
        self.assertEqual(result["outcome"], "nothingToReset")
        self.assertEqual(len(self.consumes()), 1)

    def test_no_credits_is_an_error_without_a_request(self):
        self.set_state(self.home, used=(100, 40), credits=0)
        with self.assertRaisesRegex(accounts.AccountError, "no reset credits"):
            self.reset()
        self.assertEqual(self.consumes(), [])

    def test_resets_an_added_account_in_its_own_home(self):
        alt = self.add_account("alt")
        self.set_state(alt, used=(100, 100), credits=1)
        self.assertEqual(self.reset("alt")["outcome"], "reset")
        self.assertEqual(self.consumes()[0]["codex_home"], str(alt))


class AccountManagementTest(AccountsBase):
    def test_lists_default_first_then_signed_in_accounts(self):
        self.add_account("zeta")
        self.add_account("alpha")
        (self.accounts / "pending").mkdir()  # login never finished
        self.assertEqual(accounts.list_accounts(), ["default", "alpha", "zeta"])

    def test_device_login_for_a_new_account_uses_its_own_home_and_a_file_login(self):
        info = core.start_device_login(codex_bin=FAKE, account="work")
        core.cancel_device_login()
        self.assertEqual((info["account"], info["code"]), ("work", "ABCD-12345"))
        [login] = [json.loads(p.read_text()) for p in self.logs.glob("login-*.json")]
        self.assertEqual(login["codex_home"], str(self.accounts / "work"))
        self.assertIn('cli_auth_credentials_store="file"', login["login"])
        self.assertEqual(oct((self.accounts / "work").stat().st_mode & 0o777), "0o700")

    def test_device_login_for_default_keeps_the_users_own_home(self):
        core.start_device_login(codex_bin=FAKE, codex_home=str(self.home))
        core.cancel_device_login()
        [login] = [json.loads(p.read_text()) for p in self.logs.glob("login-*.json")]
        self.assertEqual(login["codex_home"], str(self.home))
        self.assertNotIn('cli_auth_credentials_store="file"', login["login"])

    def test_login_status_reports_the_accounts_email_and_plan(self):
        self.add_account("alt", email="alt@example.com", plan="pro")
        status = core.login_status(codex_bin=FAKE, account="alt")
        self.assertEqual((status["email"], status["plan"]), ("alt@example.com", "pro"))
        self.assertEqual(core.login_status(codex_bin=FAKE, codex_home=str(self.home))["plan"], "plus")

    def test_invalid_account_names_are_rejected(self):
        for name in ("../etc", "Upper", "", "a" * 33):
            with self.assertRaises(core.CodexImageError):
                core.start_device_login(codex_bin=FAKE, account=name)

    def test_remove_account_deletes_its_home_but_never_the_default(self):
        alt = self.add_account("alt")
        accounts.mark_exhausted("alt")
        self.assertEqual(accounts.remove_account("alt"), {"removed": "alt"})
        self.assertFalse(alt.exists())
        self.assertFalse(accounts.is_exhausted("alt"))
        with self.assertRaisesRegex(accounts.AccountError, "default account"):
            accounts.remove_account("default")
        self.assertTrue((self.home / "auth.json").exists())

    def test_an_exhausted_mark_expires(self):
        accounts.mark_exhausted("default", until=time.time() - 1)
        self.assertFalse(accounts.is_exhausted("default"))


class HardeningTest(AccountsBase):
    def test_a_stalled_app_server_behind_a_wrapper_times_out(self):
        # The npm `codex` is a wrapper whose native child keeps the pipes open after a kill.
        wrapper = self.root / "codex-wrapper"
        wrapper.write_text(f"#!/bin/sh\n'{sys.executable}' '{FAKE}' \"$@\"\n")
        wrapper.chmod(0o755)
        os.environ["FAKE_HANG"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_HANG", None)
        start = time.time()
        with self.assertRaisesRegex(accounts.AccountError, "did not answer"):
            accounts.app_server_call(self.home, "account/rateLimits/read", None, str(wrapper),
                                     timeout=2)
        self.assertLess(time.time() - start, 10)

    def test_names_with_a_trailing_newline_are_rejected(self):
        with self.assertRaises(accounts.AccountError):
            accounts.account_home("work\n")

    def test_added_accounts_always_keep_their_login_in_a_file(self):
        self.add_account("alt")
        self.set_state(self.add_account("alt"), credits=0)
        self.gen(self.root / "o.png", account="alt")
        self.assertIn('cli_auth_credentials_store="file"', self.calls()[0]["argv"])
        self.usage(account="alt")
        [server] = [json.loads(p.read_text()) for p in self.logs.glob("appserver-*.json")]
        self.assertIn('cli_auth_credentials_store="file"', server["argv"])

    def test_the_default_account_runs_without_the_file_store_override(self):
        self.gen(self.root / "o.png")
        self.assertNotIn('cli_auth_credentials_store="file"', self.calls()[0]["argv"])
