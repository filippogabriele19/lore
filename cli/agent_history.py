def _compress_history(
    messages: list,
    keep_last_n: int = 1,
    tool_summaries: dict | None = None,
) -> None:
    tool_msg_indices = [
        i for i, m in enumerate(messages)
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in m["content"]
        )
    ]
    to_compress = tool_msg_indices[:-keep_last_n] if keep_last_n > 0 else tool_msg_indices

    for idx in to_compress:
        new_content = []
        for block in messages[idx]["content"]:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
                and len(block["content"]) > 120
                and not block["content"].startswith("[data delivered")
            ):
                tid = block.get("tool_use_id", "")
                if tool_summaries and tid in tool_summaries:
                    summary_text = tool_summaries[tid]
                else:
                    first_line = next(
                        (l.strip() for l in block["content"].splitlines() if l.strip()),
                        block["content"][:80],
                    )
                    summary_text = first_line[:150]
                compressed = f"[data delivered — {summary_text}]"
                new_content.append({**block, "content": compressed})
            else:
                new_content.append(block)
        messages[idx] = {**messages[idx], "content": new_content}
