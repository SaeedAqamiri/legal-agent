import unittest

from legal_agent_core.canonical import ProvisionType
from legal_agent_core.errors import DomainError
from legal_agent_core.ingestion.markdown_parser import (
    extract_provisions,
    markdown_title,
    parse_markdown_document,
    parse_pages,
)

SAMPLE = """# D01 — دستورالعمل اجرایی هیئت‌های تشخیص مطالبات

- Source PDF: D01.pdf
- Physical pages: 2

## صفحه 1

ماده 1 کارفرما موظف است حق بیمه را به سازمان پرداخت نماید.
تبصره 1 در موارد خاص مهلت قابل تمدید است.

## صفحه 2

بخش ۲ نحوه تشکیل جلسات تعیین می‌شود.
ارائه اسناد از سوی هیئت الزامی است.
"""


class MarkdownParserTests(unittest.TestCase):
    def test_parse_pages_assigns_page_numbers(self) -> None:
        lines = parse_pages(SAMPLE)
        pages = {line.page for line in lines}
        self.assertEqual(pages, {1, 2})
        self.assertTrue(all(line.text for line in lines))

    def test_extract_provisions_splits_structural_labels(self) -> None:
        provisions = extract_provisions(parse_pages(SAMPLE))
        self.assertEqual(len(provisions), 3)
        self.assertEqual(provisions[0].label, "ماده 1")
        self.assertEqual(provisions[1].label, "تبصره 1")
        self.assertEqual(provisions[2].label, "بخش ۲")
        # text following a heading is accumulated into the provision
        self.assertIn("تشکیل جلسات", " ".join(provisions[2].text))

    def test_parse_markdown_document_builds_parsed_document(self) -> None:
        parsed = parse_markdown_document(SAMPLE, filename="D01.md")
        self.assertEqual(parsed.source.filename, "D01.md")
        self.assertEqual(parsed.source.media_type, "text/markdown")
        self.assertTrue(parsed.source.checksum.startswith("sha256:"))
        self.assertEqual(parsed.version.version_label, "v1")
        self.assertGreaterEqual(len(parsed.provisions), 3)
        by_label = {item.label: item for item in parsed.provisions}
        self.assertEqual(by_label["ماده 1"].provision_type, ProvisionType.ARTICLE)
        self.assertEqual(by_label["تبصره 1"].provision_type, ProvisionType.NOTE)
        self.assertEqual(by_label["بخش ۲"].provision_type, ProvisionType.PART)
        self.assertEqual(by_label["ماده 1"].page_number, 1)
        self.assertEqual(by_label["بخش ۲"].page_number, 2)

    def test_page_segments_split_multi_page_provisions(self) -> None:
        content = (
            "# تست\n"
            "## صفحه 3\n"
            "ماده ۱: متن صفحه سه\n"
            "ادامه در همان صفحه\n"
            "## صفحه 4\n"
            "این خط در صفحه چهار است\n"
            "## صفحه 5\n"
            "ماده ۲: متن ماده دو\n"
        )
        parsed = parse_markdown_document(content, filename="multi.md")
        article = next(item for item in parsed.provisions if item.label == "ماده ۱")
        segments = article.page_segments
        self.assertEqual(len(segments), 2)
        self.assertEqual([segment.page_number for segment in segments], [3, 4])
        self.assertIn("متن صفحه سه", segments[0].text)
        self.assertIn("صفحه چهار", segments[1].text)
        for segment in segments:
            self.assertIs(segment.text in article.raw_text, True)
            self.assertEqual(
                segment.text,
                article.raw_text[segment.char_start : segment.char_end],
            )
        # single-page provisions keep exactly one segment
        single = next(item for item in parsed.provisions if item.label == "ماده ۲")
        self.assertEqual(len(single.page_segments), 1)
        self.assertEqual(single.page_segments[0].page_number, 5)

    def test_empty_document_raises(self) -> None:
        with self.assertRaises(DomainError):
            parse_markdown_document("# فقط عنوان", filename="empty.md")

    def test_title_cleans_doc_prefix(self) -> None:
        self.assertEqual(
            markdown_title("D01 — دستورالعمل", "D01.md"),
            "دستورالعمل",
        )
        self.assertEqual(markdown_title(None, "D09.md"), "D09")


if __name__ == "__main__":
    unittest.main()
