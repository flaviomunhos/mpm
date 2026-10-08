import unittest

from mpm.launcher import choose_command, run_launcher


def scripted(*answers):
    queue = list(answers)
    return lambda _prompt: queue.pop(0) if queue else ""


class LauncherTest(unittest.TestCase):
    def choose(self, *answers, elevated=True):
        out: list[str] = []
        return choose_command(scripted(*answers), out.append, elevated), "\n".join(out)

    def test_old_pc_runs_tx(self):
        self.assertEqual(self.choose("1")[0], ["tx"])

    def test_new_pc_full_is_default_choice(self):
        self.assertEqual(self.choose("2", "1", "maria", "s")[0], ["rx", "--as-user", "maria", "--admin", "--full"])
        self.assertEqual(self.choose("2", "1", "maria", "")[0], ["rx", "--as-user", "maria", "--full"])

    def test_new_pc_profile_only_asks_name_and_admin(self):
        self.assertEqual(self.choose("2", "2", "maria", "s")[0], ["rx", "--as-user", "maria", "--admin"])
        self.assertEqual(self.choose("2", "2", "maria", "")[0], ["rx", "--as-user", "maria"])

    def test_apps_and_settings(self):
        self.assertEqual(self.choose("2", "3")[0], ["rx", "--apps", "--install"])
        self.assertEqual(self.choose("2", "4", "maria")[0], ["rx", "--settings", "--to-user", "maria"])

    def test_back_returns_to_first_menu(self):
        self.assertEqual(self.choose("2", "v", "1")[0], ["tx"])

    def test_screen_is_cleared_between_menus(self):
        clears = []
        out: list[str] = []
        args = choose_command(scripted("2", "2", "maria", ""), out.append, True, lambda: clears.append(1))
        self.assertEqual(args, ["rx", "--as-user", "maria"])
        self.assertGreaterEqual(len(clears), 3)        # menu inicial, submenu, antes de executar

    def test_second_menu_is_compact(self):
        _a, text = self.choose("2", "v", "q")
        sub = text.split("MPM · PC NOVO")[1].split("MUNHOS PC")[0]
        self.assertLessEqual(len([l for l in sub.splitlines() if l.strip()]), 6)

    def test_enter_in_new_menu_picks_recommended_full(self):
        self.assertEqual(self.choose("2", "", "maria", "")[0], ["rx", "--as-user", "maria", "--full"])

    def test_invalid_user_names_are_refused(self):
        args, text = self.choose("2", "1", "joão silva", "a" * 21, "maria", "")
        self.assertEqual(args, ["rx", "--as-user", "maria", "--full"])
        self.assertEqual(text.count("Nome inválido"), 2)

    def test_top_menu_only_old_or_new(self):
        _a, text = self.choose("q")
        self.assertIn("ANTIGO", text)
        self.assertIn("NOVO", text)
        self.assertNotIn("TUDO de uma vez", text)

    def test_empty_name_asks_again_and_invalid_option_too(self):
        args, text = self.choose("2", "2", "", "x", "v", "1")
        self.assertEqual(args, ["tx"])
        self.assertIn("Informe um nome", text)
        self.assertIn("Opção inválida", text)

    def test_quit_and_not_elevated_warning(self):
        self.assertIsNone(self.choose("q")[0])
        self.assertIsNone(self.choose("")[0])
        _args, text = self.choose("q", elevated=False)
        self.assertIn("Executar como administrador", text)
        self.assertNotIn("Executar como administrador", self.choose("q", elevated=True)[1])

    def test_run_launcher_calls_main_and_waits_for_enter(self):
        calls = []
        code = run_launcher(lambda argv: calls.append(argv) or 5, scripted("1", ""), lambda _t: None)
        self.assertEqual((code, calls), (5, [["tx"]]))

    def test_run_launcher_quit_does_not_call_main(self):
        calls = []
        self.assertEqual(run_launcher(lambda argv: calls.append(argv) or 0, scripted("q"), lambda _t: None), 0)
        self.assertEqual(calls, [])

    def test_ctrl_c_and_eof_are_quiet(self):
        def boom(_prompt):
            raise EOFError
        self.assertEqual(run_launcher(lambda argv: 0, boom, lambda _t: None), 0)
        self.assertEqual(run_launcher(lambda argv: (_ for _ in ()).throw(KeyboardInterrupt()), scripted("1"),
                                      lambda _t: None), 130)


if __name__ == "__main__":
    unittest.main()
