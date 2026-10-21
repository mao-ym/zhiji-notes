"""文稿导出的行为测试。"""

from pathlib import Path

import pytest

from writing_companion.config import Settings
from writing_companion.services.documents import DocumentService
from writing_companion.services.exporting import ExportService
from writing_companion.storage.database import Database
from writing_companion.storage.library import WritingLibrary


def make_services(tmp_path: Path) -> tuple[DocumentService, ExportService]:
    """创建共享同一临时写作库的文档与导出服务。"""

    library = WritingLibrary(Settings(library_root=tmp_path))
    paths = library.initialize()
    database = Database(paths.database_file, library_root=paths.root)
    return DocumentService(library, database), ExportService(library, database)


def test_export_markdown_writes_original_body_only_to_exports_directory(tmp_path):
    """如果 markdown 导出改写正文或写到 exports 外，本测试会失败。"""

    document_service, export_service = make_services(tmp_path)
    document = document_service.create("fragment", "片段")
    document_service.save(document.id, "# 标题\n\n正文 **加粗**", explicit=False)

    exported_path = export_service.export(document.id, "markdown")

    assert exported_path.parent == tmp_path / "exports"
    assert exported_path.suffix == ".md"
    assert exported_path.read_text(encoding="utf-8") == "# 标题\n\n正文 **加粗**"


def test_export_text_removes_markdown_syntax_without_leaving_library_root(tmp_path):
    """如果 text 导出保留 Markdown 语法或越过库根目录，本测试会失败。"""

    document_service, export_service = make_services(tmp_path)
    document = document_service.create("work", "作品")
    document_service.save(
        document.id,
        "# 标题\n\n正文 **加粗** 和 [链接](https://example.com)\n![图片](x.png)",
        explicit=False,
    )

    exported_path = export_service.export(document.id, "text")

    assert exported_path.parent == tmp_path / "exports"
    assert exported_path.suffix == ".txt"
    assert exported_path.read_text(encoding="utf-8") == "标题\n\n正文 加粗 和 链接\n图片"


def test_export_text_strips_common_markdown_without_breaking_plain_underscores(tmp_path):
    """如果列表、引用、围栏、删除线或普通下划线处理错误，本测试会失败。"""

    document_service, export_service = make_services(tmp_path)
    document = document_service.create("work", "作品")
    document_service.save(
        document.id,
        "\n".join(
            [
                "# 标题",
                "",
                "- 第一项",
                "1. 第二项",
                "> 引用 **重点** 和 *斜体*",
                "```python",
                "foo_bar_baz = 'keep'",
                "```",
                "~~删除线~~ 和 __粗体__",
                "![替代文字](../../attachments/doc/photo.png)",
                "[链接文字](https://example.com)",
            ]
        ),
        explicit=False,
    )

    exported_path = export_service.export(document.id, "text")

    assert exported_path.read_text(encoding="utf-8") == "\n".join(
        [
            "标题",
            "",
            "第一项",
            "第二项",
            "引用 重点 和 斜体",
            "foo_bar_baz = 'keep'",
            "删除线 和 粗体",
            "替代文字",
            "链接文字",
        ]
    )


def test_export_rejects_unknown_format(tmp_path):
    """如果未知导出格式被接受，本测试会失败。"""

    _document_service, export_service = make_services(tmp_path)

    with pytest.raises(ValueError):
        export_service.export("00000000-0000-0000-0000-000000000003", "pdf")
