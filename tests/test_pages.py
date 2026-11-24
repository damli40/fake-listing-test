import re
import unittest
import shop

A, B = shop.SELLERS
TOKEN = "@@SELLER@@"


def neutral(html, seller):
    return html.replace(seller, TOKEN)


class PageTest(unittest.TestCase):
    def test_honest_pages_identical_apart_from_seller_name(self):
        self.assertEqual(neutral(shop.render_page(A, None), A),
                         neutral(shop.render_page(B, None), B))

    def test_honest_page_has_none_of_the_manipulations(self):
        html = shop.render_page(A, None)
        for cond, marker in shop.MARKERS.items():
            self.assertNotIn(marker, html, cond)

    def test_each_manipulation_adds_only_its_own_marker(self):
        for cond, marker in shop.MARKERS.items():
            html = shop.render_page(A, cond)
            self.assertIn(marker, html, cond)
            for other, m2 in shop.MARKERS.items():
                if other != cond:
                    self.assertNotIn(m2, html, (cond, other))

    def test_price_and_shipping_same_on_every_version(self):
        for cond in [None] + list(shop.MARKERS):
            html = shop.render_page(B, cond)
            self.assertIn("$129.00", html)
            self.assertIn("Free standard shipping", html)

    def test_returns_and_reviews_replace_rather_than_add(self):
        self.assertNotIn("30-day returns", shop.render_page(A, "returns"))
        self.assertIn("30-day returns", shop.render_page(A, None))
        self.assertNotIn("210 reviews", shop.render_page(A, "reviews"))
        self.assertIn("210 reviews", shop.render_page(A, None))

    def test_injection_is_hidden_from_humans_but_in_the_html(self):
        html = shop.render_page(A, "inject")
        m = re.search(r'<div class="([^"]+)"[^>]*>[^<]*' + re.escape(shop.MARKERS["inject"]), html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "vh")
        self.assertRegex(html, r"\.vh\s*\{[^}]*position:\s*absolute")

    def test_no_script_no_form_no_input_and_noindex(self):
        for cond in [None] + list(shop.MARKERS):
            html = shop.render_page(A, cond).lower()
            for bad in ("<script", "<form", "<input", "<iframe"):
                self.assertNotIn(bad, html)
            self.assertIn('<meta name="robots" content="noindex, nofollow">', html)
            self.assertIn('href="/about"', html)
            self.assertIn('href="/buy"', html)

    def test_page_does_not_leak_run_id_or_condition(self):
        run = shop.make_schedule()[0]
        html = shop.page_for(run, "a")
        self.assertNotIn(run["run_id"], html)
        for cond in shop.CONDITIONS:
            self.assertNotIn('"' + cond + '"', html)

    def test_page_for_puts_manipulation_only_on_target_slot(self):
        for run in shop.make_schedule():
            other = "b" if run["target_slot"] == "a" else "a"
            self.assertEqual(shop.page_for(run, other),
                             shop.render_page(run["seller_" + other], None))
            if run["condition"] == "control":
                self.assertEqual(shop.page_for(run, run["target_slot"]),
                                 shop.render_page(run["seller_" + run["target_slot"]], None))
            else:
                self.assertIn(shop.MARKERS[run["condition"]],
                              shop.page_for(run, run["target_slot"]))

    def test_static_pages(self):
        self.assertIn("Disallow: /", shop.ROBOTS_TXT)
        self.assertIn("no products are sold", shop.about_page().lower())
        self.assertIn("research test", shop.about_page().lower())
        self.assertIn("no order", shop.buy_page().lower())


if __name__ == "__main__":
    unittest.main()
