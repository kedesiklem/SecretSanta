"""Message template and the e-mail built from it (sender.py)."""
import email
import unittest
from email import policy

from helpers import ProjectCase, Santa
import sender


class TemplateParsing(unittest.TestCase):
    def test_ok_and_variables(self):
        t = sender.Template.parse("Subject: Hi {year}\n\nTu offres à {recipient} ({santa}).\n")
        self.assertEqual(t.render(santa="A", recipient="B", year=2026), ("Hi 2026", "Tu offres à B (A).\n"))

    def test_preview_uses_the_storybook_characters(self):
        subject, body = sender.Template.parse("Subject: S\n\n{santa} -> {recipient}").preview(2026)
        self.assertEqual(body.strip(), f"{sender.PREVIEW_SANTA} -> {sender.PREVIEW_RECIPIENT}")
        self.assertEqual((sender.PREVIEW_SANTA, sender.PREVIEW_RECIPIENT), ("Mère Noël", "Rudolph"))

    def test_errors(self):
        bad = {"no subject": "Hello\n\nbody", "line 2 not empty": "Subject: S\nTo: x\n\nbody",
               "empty body": "Subject: S\n\n   \n", "unknown variable": "Subject: S\n\n{nope}",
               "stray brace": "Subject: S\n\nhello }", "bad format": "Subject: S\n\n{recipient!z}"}
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(Santa.SantaError):
                sender.Template.parse(text)

    def test_literal_braces_and_crlf(self):
        t = sender.Template.parse("Subject: S\r\n\r\nUn {{mot}} {recipient}\r\n")
        self.assertEqual(t.render(santa="A", recipient="B", year=1)[1], "Un {mot} B\n")


class TemplateLoading(ProjectCase):
    def test_default_when_missing_with_notice(self):
        t, notices = sender.Template.load(None, self.dir)
        self.assertEqual(t.source, "message par défaut")
        self.assertEqual([n.level for n in notices], ["info"])

    def test_file_is_used_and_bom_dropped(self):
        (self.dir / "message.txt").write_bytes("﻿Subject: Bonjour\n\nCorps {recipient}\n".encode("utf-8"))
        t, notices = sender.Template.load(None, self.dir)
        self.assertEqual((t.subject, notices), ("Bonjour", []))

    def test_explicit_missing_file_is_an_error(self):
        with self.assertRaises(Santa.SantaError):
            sender.Template.load("nope.txt", self.dir)


class MimeMessage(unittest.TestCase):
    def test_structure_and_encoding(self):
        t = sender.Template.parse("Subject: 🎅 Père Noël {year}\n\nTu offres à {recipient} — « merci » !\n")
        raw = sender.build_mime(t, "Élodie", "Zoé", 2026, "elodie@example.org")
        msg = email.message_from_bytes(raw, policy=policy.default)
        self.assertEqual(msg["To"], "elodie@example.org")
        self.assertEqual(msg["Subject"], "🎅 Père Noël 2026")
        self.assertIn("Zoé — « merci » !", msg.get_content())
        self.assertEqual(msg.get_content_charset(), "utf-8")


if __name__ == "__main__":
    unittest.main()
