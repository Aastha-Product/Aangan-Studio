"""Step 4: the speech guard must block every way of saying a price, and nothing else."""
import unittest

from backend.speech_guard import DEFLECTION_LINE, guard

MUST_BLOCK = [
    # what pricing.md says the agent must never say
    "It'll cost around 12 lakh for your flat.",
    "Our rates start at 1,800 per sq ft.",
    "For a 2BHK it's typically eight to ten lakhs.",
    # currency / units in many spellings
    "That would be roughly ₹15,00,000.",
    "Somewhere near Rs 15 lakh all-in.",
    "Around 20 lakh rupees.",
    "Expect about INR 900000.",
    "It's usually 2,400 per square foot for premium.",
    "Standard spec is eighteen hundred a square foot.",
    "About 1800/sq ft.",
    "A single room is 3.5 to 8 lakh.",
    "A bedroom alone is around 350k.",
    "Commercial fitouts are one point two crore and up.",
    "The design fee is 50,000.",
    "Our pricing starts from two thousand.",
    "Your budget of 1.5 would not be enough.",
    "A ballpark would be fifteen.",
    "It's cheaper, maybe 10 to 12.",
    # Hindi / Marathi
    "Lagbhag das लाख lagega.",
    "15 लाख रुपये tak.",
    "Kharcha around 8 lakh hoga.",
    # price smuggled after a booking confirmation
    "You're booked for Thursday at 11 am. By the way, kitchens start at 3 lakh.",
]

MUST_PASS = [
    DEFLECTION_LINE,
    "Good evening, Aangan Studio. This is the studio's virtual assistant — how can I help you?",
    "Design takes 3 to 4 weeks, and execution takes 8 to 16 weeks depending on size.",
    "We cannot take a project that has to be ready in under 6 weeks from today.",
    "We do offices, clinics and studios up to about 3,000 square feet.",
    "I have Thursday the 9th at 11 in the morning, or Saturday the 11th at 4 in the afternoon. Which suits you better?",
    "You're booked for Thursday 9 October at 11 am — a site visit.",
    "Where is the property? Pimple Nilakh, got it.",
    "Roughly how many square feet, carpet area?",
    "Is this number the best one to reach you on?",
    "p, r, i, y, a, dot, j, at gmail dot com — is that right?",
    "Thank you for being open about that. I want to be straight with you — for a kitchen and a bedroom with full "
    "execution, that budget would be well below what a project like this needs with us.",
    "This sounds like it may not be the right fit for us right now — but feel free to reach out if your timeline or scope changes.",
    "I've flagged this to a senior person at the studio, and they will call you back within 15 minutes.",
    "A 2BHK in Wakad with a modular kitchen — lovely.",
]


class SpeechGuard(unittest.TestCase):
    def test_blocks_every_price(self):
        for text in MUST_BLOCK:
            with self.subTest(text=text):
                r = guard(text)
                self.assertTrue(r.blocked, f"NOT blocked: {text}")
                self.assertEqual(r.text, DEFLECTION_LINE)
                self.assertTrue(r.reasons)

    def test_passes_normal_speech(self):
        for text in MUST_PASS:
            with self.subTest(text=text):
                r = guard(text)
                self.assertFalse(r.blocked, f"wrongly blocked: {text} -> {r.reasons}")
                self.assertEqual(r.text, text)

    def test_deflection_line_itself_never_blocked(self):
        self.assertFalse(guard(DEFLECTION_LINE).blocked)
        self.assertFalse(guard("I understand. " + DEFLECTION_LINE).blocked)


if __name__ == "__main__":
    unittest.main()
