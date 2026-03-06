from types import SimpleNamespace

import pytest

from src.telegram.rendering import (
    RenderProfile,
    RenderedFile,
    RenderedPhoto,
    RenderedText,
    TelegramMessageRenderer,
)


def _renderer(**kwargs) -> TelegramMessageRenderer:
    defaults = {
        "enabled": True,
        "final_only": True,
        "style": "strong",
        "mode": "html",
        "link_preview_policy": "auto",
        "fail_open": True,
        "backend": "telegramify",
        "max_chunk_chars": 240,
    }
    defaults.update(kwargs)
    return TelegramMessageRenderer(**defaults)


@pytest.mark.asyncio
async def test_builtin_markdown_like_content_renders_to_html():
    renderer = _renderer(backend="builtin")
    text = "\n".join(
        [
            "# 标题",
            "",
            "- item A",
            "- item B",
            "",
            "> 引用内容",
            "",
            "```python",
            "print(1)",
            "```",
        ]
    )
    result = await renderer.render_text(text, RenderProfile.ASSISTANT_FINAL)
    merged = "\n".join(item.text for item in result.items if isinstance(item, RenderedText))

    assert result.render_mode == "html"
    assert any(isinstance(item, RenderedText) and item.parse_mode == "HTML" for item in result.items)
    assert "<b>标题</b>" in merged
    assert "• item A" in merged
    assert "<blockquote>引用内容</blockquote>" in merged
    assert '<pre><code class="language-python">print(1)</code></pre>' in merged


@pytest.mark.asyncio
async def test_sulguk_backend_falls_back_to_builtin_html():
    renderer = _renderer(backend="sulguk")
    result = await renderer.render_text("# 标题", RenderProfile.ASSISTANT_FINAL)

    text_items = [item for item in result.items if isinstance(item, RenderedText)]
    assert result.render_mode == "html"
    assert text_items
    assert text_items[0].parse_mode == "HTML"


@pytest.mark.asyncio
async def test_builtin_unclosed_fence_does_not_crash_and_records_parse_error():
    renderer = _renderer(backend="builtin")
    result = await renderer.render_text("```python\nprint(1)", RenderProfile.ASSISTANT_FINAL)

    assert result.items
    assert result.parse_errors >= 1


@pytest.mark.asyncio
async def test_builtin_chunking_keeps_each_chunk_within_limit():
    renderer = _renderer(backend="builtin", max_chunk_chars=120)
    text = "# 标题\n\n" + ("- item line with details\n" * 80)
    result = await renderer.render_text(text, RenderProfile.ASSISTANT_FINAL)

    assert len(result.items) > 1
    assert all(len(item.text) <= 120 for item in result.items if isinstance(item, RenderedText))


@pytest.mark.asyncio
async def test_builtin_snake_case_does_not_trigger_double_underscore_bold():
    renderer = _renderer(backend="builtin")
    result = await renderer.render_text("变量名 hello_world_test 应保持原样", RenderProfile.ASSISTANT_FINAL)

    text_items = [item for item in result.items if isinstance(item, RenderedText)]
    assert text_items
    assert "hello_world_test" in text_items[0].text
    assert "<b>world</b>" not in text_items[0].text


@pytest.mark.asyncio
async def test_builtin_long_inline_html_chunks_remain_valid():
    renderer = _renderer(backend="builtin", max_chunk_chars=140)
    text = ("**very long text** " * 40).strip()

    result = await renderer.render_text(text, RenderProfile.ASSISTANT_FINAL)
    text_items = [item for item in result.items if isinstance(item, RenderedText)]

    assert len(text_items) > 1
    assert all(item.text.count("<b>") == item.text.count("</b>") for item in text_items)


@pytest.mark.asyncio
async def test_telegramify_table_renders_to_entities():
    renderer = _renderer()
    text = "\n".join(
        [
            "| Name | Value |",
            "| --- | --- |",
            "| foo | 1 |",
            "| bar | 2 |",
        ]
    )

    result = await renderer.render_text(text, RenderProfile.COMMAND_STATUS)
    text_items = [item for item in result.items if isinstance(item, RenderedText)]
    merged = "\n".join(item.text for item in text_items)
    entity_types = {entity.type for item in text_items for entity in (item.entities or [])}

    assert result.render_mode == "telegramify"
    assert text_items
    assert all(item.parse_mode is None for item in text_items)
    assert "Name" in merged
    assert "foo" in merged
    assert "pre" in entity_types


@pytest.mark.asyncio
async def test_telegramify_non_final_renders_text_only():
    renderer = _renderer()
    result = await renderer.render_text("# 标题\n\n- a\n- b", RenderProfile.COMMAND_HELP)

    assert result.items
    assert all(isinstance(item, RenderedText) for item in result.items)
    assert all(item.parse_mode is None for item in result.items)
    assert any(item.entities for item in result.items)


@pytest.mark.asyncio
async def test_link_preview_policy_off_sets_disable_web_page_preview():
    renderer = _renderer(link_preview_policy="off")
    result = await renderer.render_text("访问 https://example.com 获取详情", RenderProfile.ASSISTANT_FINAL)

    assert result.items
    assert all(item.disable_web_page_preview is True for item in result.items)


@pytest.mark.asyncio
async def test_telegramify_final_can_emit_file_and_photo(monkeypatch):
    renderer = _renderer()

    async def _fake_render(_: str, __: int) -> list[object]:
        return [
            SimpleNamespace(
                content_type=SimpleNamespace(value="text"),
                text="plain text",
                entities=[],
            ),
            SimpleNamespace(
                content_type=SimpleNamespace(value="file"),
                file_name="code.py",
                file_data=b"print(1)\n",
                caption_text="file caption",
                caption_entities=[],
            ),
            SimpleNamespace(
                content_type=SimpleNamespace(value="photo"),
                file_name="diagram.webp",
                file_data=b"binary",
                caption_text="photo caption",
                caption_entities=[],
            ),
        ]

    monkeypatch.setattr(renderer, "_telegramify_render", _fake_render)

    result = await renderer.render_text("mock", RenderProfile.ASSISTANT_FINAL)

    assert isinstance(result.items[0], RenderedText)
    assert isinstance(result.items[1], RenderedFile)
    assert isinstance(result.items[2], RenderedPhoto)
