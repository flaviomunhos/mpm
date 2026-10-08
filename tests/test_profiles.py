import unittest

from mpm.core.models import ProfileInfo
from mpm.core.profiles import ProfileNotFound, list_profiles, select_profile

PROFILES = [
    ProfileInfo("master", "S-1-5-21-1-1-1-1002", r"C:\Users\master", is_current=True),
    ProfileInfo("Maria", "S-1-5-21-1-1-1-1003", r"C:\Users\Maria"),
]


class SelectProfileTest(unittest.TestCase):
    def test_default_is_current(self):
        self.assertEqual(select_profile(None, PROFILES).username, "master")

    def test_by_username_case_insensitive(self):
        self.assertEqual(select_profile("maria", PROFILES).username, "Maria")

    def test_by_sid(self):
        self.assertEqual(select_profile("S-1-5-21-1-1-1-1003", PROFILES).username, "Maria")

    def test_unknown_lists_available(self):
        with self.assertRaises(ProfileNotFound) as ctx:
            select_profile("joao", PROFILES)
        self.assertIn("master", str(ctx.exception))

    def test_no_current_profile(self):
        others = [ProfileInfo("x", "1", "/x")]
        with self.assertRaises(ProfileNotFound):
            select_profile(None, others)


class RealPlatformTest(unittest.TestCase):
    def test_list_profiles_returns_profiles(self):
        profiles = list_profiles()
        self.assertTrue(profiles)
        self.assertTrue(all(p.username and p.path for p in profiles))


if __name__ == "__main__":
    unittest.main()
