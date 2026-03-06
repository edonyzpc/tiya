from src.telegram.rendering import RenderProfile, TelegramMessageRenderer


def _renderer(**kwargs) -> TelegramMessageRenderer:
    defaults = {
        "enabled": True,
        "final_only": True,
        "style": "strong",
        "mode": "html",
        "link_preview_policy": "auto",
        "fail_open": True,
        "backend": "builtin",
        "max_chunk_chars": 240,
    }
    defaults.update(kwargs)
    return TelegramMessageRenderer(**defaults)


def test_markdown_like_content_renders_to_html():
    renderer = _renderer()
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
    result = renderer.render_text(text, RenderProfile.ASSISTANT_FINAL)
    merged = "\n".join(chunk.text for chunk in result.chunks)

    assert result.render_mode == "html"
    assert any(chunk.parse_mode == "HTML" for chunk in result.chunks)
    assert "<b>标题</b>" in merged
    assert "• item A" in merged
    assert "<blockquote>引用内容</blockquote>" in merged
    assert '<pre><code class="language-python">print(1)</code></pre>' in merged


def test_unclosed_fence_does_not_crash_and_records_parse_error():
    renderer = _renderer()
    result = renderer.render_text("```python\nprint(1)", RenderProfile.ASSISTANT_FINAL)

    assert result.chunks
    assert result.parse_errors >= 1


def test_chunking_keeps_each_chunk_within_limit():
    renderer = _renderer(max_chunk_chars=120)
    text = "# 标题\n\n" + ("- item line with details\n" * 80)
    result = renderer.render_text(text, RenderProfile.ASSISTANT_FINAL)

    assert len(result.chunks) > 1
    assert all(len(chunk.text) <= 120 for chunk in result.chunks)


def test_link_preview_policy_off_sets_disable_web_page_preview():
    renderer = _renderer(link_preview_policy="off")
    result = renderer.render_text("访问 https://example.com 获取详情", RenderProfile.ASSISTANT_FINAL)

    assert result.chunks
    assert all(chunk.disable_web_page_preview is True for chunk in result.chunks)


def test_snake_case_does_not_trigger_double_underscore_bold():
    renderer = _renderer()
    result = renderer.render_text("变量名 hello_world_test 应保持原样", RenderProfile.ASSISTANT_FINAL)

    assert result.chunks
    assert "hello_world_test" in result.chunks[0].text
    assert "<b>world</b>" not in result.chunks[0].text


def test_long_inline_html_chunks_remain_valid():
    renderer = _renderer(max_chunk_chars=140)
    text = ("**very long text** " * 40).strip()

    result = renderer.render_text(text, RenderProfile.ASSISTANT_FINAL)

    assert len(result.chunks) > 1
    assert all(chunk.text.count("<b>") == chunk.text.count("</b>") for chunk in result.chunks)
